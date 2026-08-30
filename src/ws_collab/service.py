"""The shared WS_COLLAB service layer.

Every essential capability lives here exactly once. REST and WebSocket are thin
transports over this object, which is what guarantees parity: an event written
through either transport is durably appended, fanned out to live subscribers, and
visible to the other transport immediately, with identical IDs, cursors,
idempotency, filters, validation, auditing, and worker/routing/prompt logic.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .audio.capture import CaptureService
from .audio.secondary_capture import SecondaryCaptureService
from .audio.devices import DeviceRegistry
from .audio.routing import RoutingManager
from .audio.segment import AudioSegment
from .admin_ui_state import AdminUIState
from .classify import SourceClassifier
from .config import Config, ECHO_POLICIES
from .cursors import CursorManager
from .disambiguator import build_disambiguator
from .errors import AuthorizationError, ConflictError, NotFoundError, ValidationError
from .events import (
    AGENT_SPEECH_STARTED,
    CONVERSATION_MESSAGE,
    DYNAMIC_STREAMS,
    HEARD_SPEECH,
    STREAM_AUDIT,
    STREAM_CONVERSATION,
    STREAM_DIAGNOSTICS,
    STREAM_STT_TRANSCRIPTS,
    STREAM_TRANSLATED_AUDIO,
    STREAM_TTS,
    STREAM_ROLES,
    STREAM_STATUSES,
    STREAM_PURPOSES,
    STREAMS,
    STT_ENGINE_ERROR,
    STT_FINAL_RESULT,
    STT_PARTIAL_RESULT,
    TRANSCRIPT_FILTERED,
    TRANSCRIPT_RESOLVED,
    TTS_AUDIO_DETECTED_BY_MICROPHONE,
    TTS_TRANSCRIPTION_EVALUATED,
    Event,
    utc_now_iso,
    validate_new_event,
)
from .notify import Broker
from .prompt import PromptManager
from .sound_settings import SoundSettings
from .stt import build_engines, run_stt
from .stt.base import Hypothesis
from .tts import accuracy as accuracy_metrics
from .tts.engine import TtsEngine
from .tts.voices import VoiceManager
from .workers import WorkerMonitor
from . import __version__
from .meet_bridge.cdp import (
    DEFAULT_POPUP_PORT,
    DEFAULT_PROFILE,
    browser_profile_root,
    cdp_alive,
    find_browser,
    find_add_account_tab,
    find_sso_tab,
    find_sso_connector_tab,
    foreground_sso_tab,
    reuse_or_open_tab,
    scan_signed_in_sso_accounts,
)
from .meet_browser_settings import MeetBrowserSettings


# The canonical capture source that STT engine routes hang off.
DEFAULT_ROUTE_SOURCE = "microphone"

# Safety ceiling for a single read scan. `limit` is meant to bound the PRODUCED
# stream, not the pre-filter read window, so virtual/merge streams scan sources up
# to this ceiling (effectively "all") and only truncate the final result.
_MAX_SCAN = 100_000
MEET_ROLES = ("host", "companion", "guest")
_MEET_URL_RE = re.compile(
    r"https?://meet\.google\.com/([a-z]{3,4}-[a-z]{3,5}-[a-z]{3,4})(?:[/?#][^\s\"'<)]*)?",
    re.IGNORECASE,
)
_IGNORED_MEET_ROOMS = {"xxx-yyyy-zzz"}


def _sso_sort_key(account_id: str) -> tuple[int, str]:
    match = re.fullmatch(r"sso_(\d+)", str(account_id or "").strip().lower())
    return (int(match.group(1)) if match else 10_000_000, str(account_id or ""))


def _coerce_authuser(value: Any) -> int | None:
    try:
        authuser = int(value)
    except (TypeError, ValueError):
        return None
    return authuser if authuser >= 0 else None


def _markdown_title(path: Path) -> str:
    """First heading of a markdown file, for a readable document list."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            for _ in range(40):
                line = handle.readline()
                if not line:
                    break
                if line.startswith("#"):
                    return line.lstrip("#").strip()
    except OSError:
        pass
    return path.stem.replace("_", " ").title()


def _build_predicate(filters: dict[str, Any] | None) -> Callable[[Event], bool] | None:
    if not filters:
        return None
    types = set(filters.get("types") or ([filters["type"]] if filters.get("type") else []))
    source_id = filters.get("source_id")
    source_kind = filters.get("source_kind")
    correlation_id = filters.get("correlation_id")
    since = filters.get("since")
    until = filters.get("until")
    text = (filters.get("text") or "").lower()

    def predicate(event: Event) -> bool:
        if types and event.type not in types:
            return False
        if source_id and event.source_id != source_id:
            return False
        if source_kind and event.source_kind != source_kind:
            return False
        if correlation_id and event.correlation_id != correlation_id:
            return False
        if since and event.ts < since:
            return False
        if until and event.ts > until:
            return False
        if text:
            haystack = f"{event.type} {event.source_id} {event.data}".lower()
            if text not in haystack:
                return False
        return True

    return predicate


class WsCollabService:
    def __init__(self, config: Config, store):
        self.config = config
        self.store = store
        self.started_at = time.time()
        # Unique per process start. Clients compare this across (re)connects and
        # reload themselves when it changes, so a restart swaps in freshly hosted
        # assets instead of leaving stale HTML/JS running against the new server.
        self.boot_id = uuid.uuid4().hex
        self.broker = Broker()

        # Audit sink used by every subsystem so security-relevant changes are durable.
        self._audit_sink = lambda payload: self.publish(
            stream=STREAM_AUDIT,
            type=str(payload.get("type", "AUDIT")),
            data=payload,
            source_kind="system",
            source_id="system",
        )

        self.cursors = CursorManager(config.cursors_dir, audit_sink=self._audit_sink)
        self.devices = DeviceRegistry(config)
        self.routing = RoutingManager(config.state_dir, audit_sink=self._audit_sink)
        self.sound_settings = SoundSettings(config.state_dir)
        self.meet_browser_settings = MeetBrowserSettings(config.state_dir)
        self._known_meeting_scan_at = 0.0
        self._known_meeting_scan_cache: list[str] = []
        self.admin_ui_state = AdminUIState(config.state_dir)
        self._meet_bridge_process: subprocess.Popen[Any] | None = None
        self.voices = VoiceManager(config, config.state_dir, audit_sink=self._audit_sink)
        self.workers = WorkerMonitor(config, self.publish, announce=self._announce)
        self.classifier = SourceClassifier(config.echo_policy)
        self.disambiguator = build_disambiguator(config)
        self.stt_engines, self.stt_warnings = build_engines(config)
        self.tts = TtsEngine(config, self.publish)
        self.capture = CaptureService(
            config, self.devices, self.publish, self.process_segment, is_tts_speaking=lambda: self.tts.is_speaking
        )
        self.secondary_capture = SecondaryCaptureService(config, self.devices, self.publish, self.process_segment)
        # Restore the operator's persisted capture-device choice so a restart
        # resumes on the same input instead of the config/system default.
        saved_capture_device = self.sound_settings.get("capture_device")
        if saved_capture_device:
            self.capture.set_preferred_device(saved_capture_device)
        saved_echo_policy = self.sound_settings.get("echo_policy")
        if saved_echo_policy in ECHO_POLICIES:
            self.config.echo_policy = saved_echo_policy
            self.classifier.echo_policy = saved_echo_policy
        self.prompt = PromptManager(config, self.publish, read_history=self._prompt_history_events)
        self.accuracy = accuracy_metrics.AccuracyAccumulator()

        self._monitor_task: asyncio.Task | None = None
        self._warnings = list(config.warnings) + list(self.stt_warnings)

        # Client-created ("dynamic") mailboxes the server hosts, restored from a
        # durable registry so they survive restarts.
        self._dynamic_mailboxes: dict[str, dict[str, Any]] = {}
        self._load_mailbox_registry()

        # Agent (user) registry: arbitrary per-identity properties, durable.
        self._agents: dict[str, dict[str, Any]] = {}
        self._load_agent_registry()

        # Virtual (emulated) read-only mailboxes: name -> spec. Each projects a
        # source (a disk JSON file, a self:/http endpoint, or a merge: of other
        # mailboxes) as a read-only mailbox. server-agents is the default entry.
        # Runtime-created ones (e.g. a saved merge combo) are durable and marked
        # runtime="1"; config entries are re-applied on top and win on clashes.
        self._virtual: dict[str, dict[str, Any]] = {}
        self._load_virtual_registry()
        for entry in (getattr(config, "virtual_mailboxes", None) or []):
            if entry.get("mailbox") and entry.get("source"):
                spec: dict[str, Any] = {
                    "source": str(entry.get("source", "")),
                    "purpose": str(entry.get("purpose", "")),
                }
                if isinstance(entry.get("rules"), list):
                    spec["rules"] = [rule for rule in entry.get("rules", []) if isinstance(rule, dict)]
                if entry.get("policy"):
                    spec["policy"] = str(entry.get("policy"))
                self._virtual[str(entry.get("mailbox"))] = spec
        # Global namespace prefix for this server's mailboxes (federation).
        self._global_name = str(getattr(config, "global_name", "") or "").strip()

        # Per-stream, per-field cache of the last 16 distinct values seen (plus each
        # field's inferred value type), so the render/filter pickers can offer
        # candidates and later use them intelligently. Durable on disk.
        self._field_cache: dict[str, dict[str, list[str]]] = {}
        self._field_types: dict[str, dict[str, str]] = {}
        self._definition_field_cache: dict[str, dict[str, list[str]]] = {}
        self._definition_field_types: dict[str, dict[str, str]] = {}
        # Per-field cache-limit overrides, layered: a per-(stream,field) override wins
        # over a global by-field override ("cache-overrides"), which wins over the
        # default cached_limit.
        self._field_overrides_global: dict[str, int] = {}
        self._field_overrides_stream: dict[str, dict[str, int]] = {}
        self._field_overrides_observation: dict[str, dict[str, int]] = {}
        self._field_overrides_observation_stream: dict[str, dict[str, dict[str, int]]] = {}
        self._field_cache_dirty = False
        self._field_cache_saved_at = 0.0
        self._load_field_cache()
        # Rewrite legacy config into the scoped observation schema on startup.
        self._save_cache_config()

    def _meet_bridge_health(self, timeout: float = 0.5) -> dict[str, Any] | None:
        import urllib.request

        try:
            with urllib.request.urlopen("http://127.0.0.1:48699/health", timeout=timeout) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            return None

    def _meet_bridge_port_open(self) -> bool:
        import socket

        try:
            with socket.create_connection(("127.0.0.1", 48699), timeout=0.25):
                return True
        except OSError:
            return False

    def _meet_bridge_pid_path(self) -> Path:
        return Path(self.config.state_dir) / "meet_bridge.pid"

    def _is_pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            process_query_limited_information = 0x1000
            synchronize = 0x00100000
            wait_timeout = 0x00000102
            handle = kernel32.OpenProcess(process_query_limited_information | synchronize, False, int(pid))
            if not handle:
                return False
            try:
                return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _meet_bridge_tracked_process_running(self) -> bool:
        process = getattr(self, "_meet_bridge_process", None)
        if process is None:
            return False
        try:
            running = process.poll() is None
        except Exception:
            running = False
        if running:
            return True
        self._meet_bridge_process = None
        self._cleanup_stale_meet_bridge_pid(process.pid if getattr(process, "pid", None) else None)
        return False

    def _meet_bridge_pid_running(self) -> int | None:
        pid_path = self._meet_bridge_pid_path()
        try:
            raw = pid_path.read_text(encoding="utf-8").strip()
            pid = int(raw)
        except OSError:
            return None
        except ValueError:
            self._cleanup_stale_meet_bridge_pid()
            return None
        if self._is_pid_alive(pid):
            return pid
        self._cleanup_stale_meet_bridge_pid(pid)
        return None

    def _cleanup_stale_meet_bridge_pid(self, pid: int | None = None) -> None:
        pid_path = self._meet_bridge_pid_path()
        try:
            raw = pid_path.read_text(encoding="utf-8").strip()
            current = int(raw)
        except (OSError, ValueError):
            current = None
        if pid is not None and current is not None and current != pid:
            return
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _write_meet_bridge_pid(self, pid: int) -> None:
        pid_path = self._meet_bridge_pid_path()
        try:
            pid_path.parent.mkdir(parents=True, exist_ok=True)
            pid_path.write_text(str(int(pid)), encoding="utf-8")
        except OSError:
            pass

    def _meet_profile_path(self) -> Path:
        return Path(str(self.meet_browser_settings.get("profile_path") or DEFAULT_PROFILE)).expanduser()

    def _meet_profile_state(self, profile_path: Path | None = None) -> dict[str, Any]:
        return self.meet_browser_settings.get_profile_state(profile_path or self._meet_profile_path())

    def _set_meet_profile_state(
        self,
        profile_path: Path | None = None,
        *,
        accounts: dict[str, Any] | None = None,
        role_account_map: dict[str, Any] | None = None,
        meeting_role_account_maps: dict[str, Any] | None = None,
        companion_click: dict[str, Any] | None = None,
        meeting_companion_click: dict[str, Any] | None = None,
        known_meeting_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.meet_browser_settings.set_profile_state(
            profile_path or self._meet_profile_path(),
            accounts=accounts,
            role_account_map=role_account_map,
            meeting_role_account_maps=meeting_role_account_maps,
            companion_click=companion_click,
            meeting_companion_click=meeting_companion_click,
            known_meeting_urls=known_meeting_urls,
        )

    def _normalize_sso_accounts(self, accounts: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        cleaned: dict[str, dict[str, Any]] = {}
        if not isinstance(accounts, dict):
            return cleaned
        for account_id, payload in accounts.items():
            key = str(account_id or "").strip()
            if not key:
                continue
            row = payload if isinstance(payload, dict) else {}
            cleaned[key] = {
                "id": key,
                "email": str(row.get("email") or "").strip() or None,
                "label": str(row.get("label") or "").strip() or None,
                "authuser": _coerce_authuser(row.get("authuser")),
            }
        return cleaned

    def _normalize_role_account_map(self, mapping: dict[str, Any] | None, accounts: dict[str, dict[str, Any]]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        if isinstance(mapping, dict):
            for role_name in MEET_ROLES:
                value = str(mapping.get(role_name) or "").strip()
                if not value or value not in accounts:
                    continue
                cleaned[role_name] = value
        return cleaned

    def _persist_live_sso_accounts(self, health: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        profile_path = Path(str(((health or {}).get("hostProfile") or {}).get("path") or self._meet_profile_path())).expanduser()
        state = self._meet_profile_state(profile_path)
        stored_accounts = self._normalize_sso_accounts(state.get("accounts"))
        live_rows = (health or {}).get("ssoAccounts") or []
        existing_numbers = [
            number
            for account_id in stored_accounts
            if (number := _sso_sort_key(account_id)[0]) < 10_000_000
        ]
        next_num = max(existing_numbers or [0]) + 1
        by_email = {
            str(row.get("email") or "").strip().lower(): account_id
            for account_id, row in stored_accounts.items()
            if str(row.get("email") or "").strip()
        }
        by_authuser = {
            int(row["authuser"]): account_id
            for account_id, row in stored_accounts.items()
            if row.get("authuser") is not None
        }
        for raw in live_rows:
            if not isinstance(raw, dict):
                continue
            authuser = _coerce_authuser(raw.get("authuser"))
            email = str(raw.get("email") or "").strip().lower()
            account_id = by_email.get(email) if email else None
            if account_id is None and authuser is not None:
                candidate = by_authuser.get(authuser)
                candidate_email = str((stored_accounts.get(candidate or "") or {}).get("email") or "").strip().lower()
                if not email or not candidate_email:
                    account_id = candidate
            if account_id is None:
                account_id = f"sso_{next_num}"
                next_num += 1
            merged = stored_accounts.get(account_id, {"id": account_id})
            if email:
                merged["email"] = email
            label = str(raw.get("label") or "").strip()
            if label:
                merged["label"] = label
            if authuser is not None:
                for other_id, other in stored_accounts.items():
                    if other_id != account_id and _coerce_authuser(other.get("authuser")) == authuser:
                        other["authuser"] = None
                merged["authuser"] = authuser
            stored_accounts[account_id] = merged
            if email:
                by_email[email] = account_id
            if authuser is not None:
                by_authuser[authuser] = account_id
        self._set_meet_profile_state(profile_path, accounts=stored_accounts)
        return stored_accounts

    def _sso_accounts(self, health: dict[str, Any] | None = None, *, profile_path: Path | None = None) -> dict[str, dict[str, Any]]:
        path = Path(profile_path or self._meet_profile_path()).expanduser()
        if health:
            accounts = self._persist_live_sso_accounts(health)
            if live_path := ((health.get("hostProfile") or {}).get("path")):
                if Path(str(live_path)).expanduser() == path:
                    return accounts
        state = self._meet_profile_state(path)
        accounts = self._normalize_sso_accounts(state.get("accounts"))
        if accounts != state.get("accounts"):
            self._set_meet_profile_state(path, accounts=accounts)
        return accounts

    def _sso_account_rows(self, accounts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {**accounts[account_id], "id": account_id}
            for account_id in sorted(accounts, key=_sso_sort_key)
        ]

    def _meet_role_account_map(self, accounts: dict[str, dict[str, Any]], profile_path: Path | None = None) -> dict[str, str]:
        path = Path(profile_path or self._meet_profile_path()).expanduser()
        state = self._meet_profile_state(path)
        role_account_map = self._normalize_role_account_map(state.get("role_account_map"), accounts)
        if role_account_map != state.get("role_account_map"):
            self._set_meet_profile_state(path, role_account_map=role_account_map)
        return role_account_map

    @staticmethod
    def _meet_assignment_key(meeting_url: str) -> str:
        value = str(meeting_url or "").strip()
        room_match = re.fullmatch(r"[a-z]{3,4}-[a-z]{3,5}-[a-z]{3,4}", value, re.IGNORECASE)
        if room_match:
            return f"https://meet.google.com/{room_match.group(0).lower()}"
        match = re.match(
            r"^https?://meet\.google\.com/([a-z0-9-]+)(?:[/?#].*)?$",
            value,
            re.IGNORECASE,
        )
        if not match:
            raise ValidationError("meeting_url must be a Google Meet room URL or room id")
        return f"https://meet.google.com/{match.group(1).lower()}"

    @staticmethod
    def _normal_meet_url(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        match = _MEET_URL_RE.search(value)
        if not match:
            return None
        room = match.group(1).lower()
        if room in _IGNORED_MEET_ROOMS:
            return None
        return f"https://meet.google.com/{room}"

    def _collect_meet_urls_from_value(
        self,
        value: Any,
        found: dict[str, str],
        source: str,
        *,
        depth: int = 0,
    ) -> None:
        if depth > 12:
            return
        if isinstance(value, str):
            for match in _MEET_URL_RE.finditer(value):
                url = self._normal_meet_url(match.group(0))
                if url:
                    found.setdefault(url, source)
            return
        if isinstance(value, dict):
            for key, child in value.items():
                self._collect_meet_urls_from_value(key, found, source, depth=depth + 1)
                self._collect_meet_urls_from_value(child, found, source, depth=depth + 1)
            return
        if isinstance(value, list):
            for child in value:
                self._collect_meet_urls_from_value(child, found, source, depth=depth + 1)

    def _profile_under_state_dir(self, profile_path: Path) -> bool:
        try:
            profile_path.resolve().relative_to(Path(self.config.state_dir).resolve())
            return True
        except (OSError, ValueError):
            return False

    def _collect_meet_urls_from_profile_history(self, profile_path: Path, found: dict[str, str]) -> None:
        candidates = [Path(self.config.state_dir) / "meet_bridge_profile"]
        if self._profile_under_state_dir(profile_path):
            candidates.append(profile_path)
        for profile in candidates:
            if not profile.exists():
                continue
            for pattern in ("History", "Tabs_*"):
                for path in profile.rglob(pattern):
                    if not path.is_file():
                        continue
                    try:
                        if path.stat().st_size > 64 * 1024 * 1024:
                            continue
                        text = path.read_bytes().decode("utf-8", errors="ignore")
                    except OSError:
                        continue
                    self._collect_meet_urls_from_value(text, found, f"browser profile {path.name}")

    def _historical_meet_urls(self, profile_path: Path) -> dict[str, str]:
        found: dict[str, str] = {}
        for stream in [*STREAMS, *self._dynamic_mailboxes]:
            try:
                for event in self.store.tail(stream, _MAX_SCAN):
                    self._collect_meet_urls_from_value(event.to_dict(), found, f"{stream} event history")
            except Exception:
                continue
        try:
            admin_state = self.admin_ui_state.get_page("meet").get("state", {})
            self._collect_meet_urls_from_value(admin_state, found, "admin UI state")
        except Exception:
            pass
        self._collect_meet_urls_from_profile_history(profile_path, found)
        return found

    def _remember_known_meeting_urls(
        self,
        profile_path: Path,
        urls: list[str] | tuple[str, ...] | set[str],
    ) -> list[str]:
        state = self._meet_profile_state(profile_path)
        known: list[str] = []
        seen: set[str] = set()
        raw_known = state.get("known_meeting_urls", [])
        raw_maps = state.get("meeting_role_account_maps", {})
        raw_click_maps = state.get("meeting_companion_click", {})
        candidates: list[Any] = []
        if isinstance(raw_known, list):
            candidates.extend(raw_known)
        if isinstance(raw_maps, dict):
            candidates.extend(raw_maps.keys())
        if isinstance(raw_click_maps, dict):
            candidates.extend(raw_click_maps.keys())
        candidates.extend(urls)
        for candidate in candidates:
            url = self._normal_meet_url(candidate)
            if url and url not in seen:
                seen.add(url)
                known.append(url)
        if known != raw_known:
            self._set_meet_profile_state(profile_path, known_meeting_urls=known)
        return known

    def _known_meeting_urls(self, profile_path: Path, *, include_history: bool = True) -> list[str]:
        state = self._meet_profile_state(profile_path)
        known = self._remember_known_meeting_urls(profile_path, set())
        if include_history:
            now = time.time()
            if not self._known_meeting_scan_cache or now - self._known_meeting_scan_at > 60.0:
                recovered = self._historical_meet_urls(profile_path)
                self._known_meeting_scan_cache = sorted(recovered)
                self._known_meeting_scan_at = now
            known = self._remember_known_meeting_urls(profile_path, set(self._known_meeting_scan_cache))
        live_url = self._normal_meet_url((self._meet_bridge_health_for_settings() or {}).get("meetingUrl"))
        if live_url:
            known = self._remember_known_meeting_urls(profile_path, {live_url})
        return known

    def _meeting_role_overrides(
        self,
        accounts: dict[str, dict[str, Any]],
        profile_path: Path,
        meeting_url: str,
    ) -> dict[str, str | None]:
        key = self._meet_assignment_key(meeting_url)
        raw_maps = self._meet_profile_state(profile_path).get("meeting_role_account_maps", {})
        raw = raw_maps.get(key, {}) if isinstance(raw_maps, dict) else {}
        if not isinstance(raw, dict):
            return {}
        overrides: dict[str, str | None] = {}
        for role in MEET_ROLES:
            if role not in raw:
                continue
            account_id = str(raw.get(role) or "").strip()
            if account_id in accounts:
                overrides[role] = account_id
        return overrides

    def _effective_meet_role_account_map(
        self,
        accounts: dict[str, dict[str, Any]],
        profile_path: Path,
        meeting_url: str = "",
    ) -> tuple[dict[str, str], dict[str, str | None]]:
        effective = self._meet_role_account_map(accounts, profile_path)
        overrides: dict[str, str | None] = {}
        if meeting_url:
            overrides = self._meeting_role_overrides(accounts, profile_path, meeting_url)
            for role, account_id in overrides.items():
                if account_id:
                    effective[role] = account_id
                else:
                    effective.pop(role, None)
        return effective, overrides

    @staticmethod
    def _normalize_companion_click_setting(raw: Any, default: dict[str, Any] | None = None) -> dict[str, Any]:
        baseline = default or {
            "enabled": False,
            "intervalSeconds": 2.0,
            "mode": "reactive",
            "trigger": "caption",
            "afterSeconds": 10.0,
            "silenceMs": 500.0,
            "minGapSeconds": 6.0,
            "maxWaitSeconds": 0.0,
            "audioRmsThreshold": 0.015,
            "clickMs": 100.0,
            "gain": 0.12,
            "sound": "uh",
            "f0Hz": 125.0,
            "f1Hz": 600.0,
            "f2Hz": 1300.0,
        }
        row = raw if isinstance(raw, dict) else {}
        enabled = bool(row.get("enabled", baseline.get("enabled", False)))
        aliases = {
            "intervalSeconds": ("intervalSeconds", "interval_seconds"),
            "afterSeconds": ("afterSeconds", "after_seconds"),
            "silenceMs": ("silenceMs", "silence_ms"),
            "minGapSeconds": ("minGapSeconds", "min_gap_seconds"),
            "maxWaitSeconds": ("maxWaitSeconds", "max_wait_seconds"),
            "audioRmsThreshold": ("audioRmsThreshold", "audio_rms_threshold"),
            "clickMs": ("clickMs", "click_ms"),
            "gain": ("gain",),
            "f0Hz": ("f0Hz", "f0_hz", "f0"),
            "f1Hz": ("f1Hz", "f1_hz", "f1"),
            "f2Hz": ("f2Hz", "f2_hz", "f2"),
        }

        def positive(name: str, fallback: float) -> float:
            raw_value = next((row[key] for key in aliases[name] if key in row), baseline.get(name, fallback))
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                value = float(baseline.get(name, fallback))
            return value if value > 0 else float(baseline.get(name, fallback))

        def nonnegative(name: str, fallback: float) -> float:
            raw_value = next((row[key] for key in aliases[name] if key in row), baseline.get(name, fallback))
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                value = float(baseline.get(name, fallback))
            return value if value >= 0 else float(baseline.get(name, fallback))

        mode = str(row.get("mode") or baseline.get("mode") or "reactive").lower()
        if mode not in {"reactive", "fixed"}:
            mode = "reactive"
        trigger = str(row.get("trigger") or baseline.get("trigger") or "caption").lower()
        if trigger not in {"caption", "audio", "both"}:
            trigger = "caption"
        sound = str(row.get("sound") or baseline.get("sound") or "uh").lower()
        if sound not in {"uh", "click"}:
            sound = "uh"
        return {
            "enabled": enabled,
            "intervalSeconds": positive("intervalSeconds", 2.0),
            "mode": mode,
            "trigger": trigger,
            "afterSeconds": positive("afterSeconds", 10.0),
            "silenceMs": positive("silenceMs", 500.0),
            "minGapSeconds": positive("minGapSeconds", 6.0),
            "maxWaitSeconds": nonnegative("maxWaitSeconds", 0.0),
            "audioRmsThreshold": nonnegative("audioRmsThreshold", 0.015),
            "clickMs": positive("clickMs", 100.0),
            "gain": min(1.0, positive("gain", 0.12)),
            "sound": sound,
            "f0Hz": positive("f0Hz", 125.0),
            "f1Hz": positive("f1Hz", 600.0),
            "f2Hz": positive("f2Hz", 1300.0),
        }

    def _meet_companion_click_maps(
        self,
        profile_path: Path,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        state = self._meet_profile_state(profile_path)
        default = self._normalize_companion_click_setting(state.get("companion_click"))
        raw_overrides = state.get("meeting_companion_click", {})
        overrides: dict[str, dict[str, Any]] = {}
        if isinstance(raw_overrides, dict):
            for raw_key, raw_setting in raw_overrides.items():
                try:
                    key = self._meet_assignment_key(str(raw_key))
                except ValidationError:
                    continue
                overrides[key] = self._normalize_companion_click_setting(raw_setting, default)
        if overrides != raw_overrides:
            self._set_meet_profile_state(profile_path, meeting_companion_click=overrides)
        return default, overrides

    def _effective_meet_companion_click(
        self,
        profile_path: Path,
        meeting_url: str = "",
    ) -> dict[str, Any]:
        default, overrides = self._meet_companion_click_maps(profile_path)
        key = self._meet_assignment_key(meeting_url) if meeting_url else ""
        if key and key in overrides:
            return {**overrides[key], "meetingUrl": key, "source": "override"}
        return {**default, "meetingUrl": key, "source": "default"}

    def _meet_role_assignments(self, accounts: dict[str, dict[str, Any]], role_account_map: dict[str, str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for role in MEET_ROLES:
            account_id = role_account_map.get(role)
            account = accounts.get(account_id or "", {})
            rows.append({
                "role": role,
                "account_id": account_id,
                "email": account.get("email"),
                "label": account.get("label"),
                "authuser": account.get("authuser"),
            })
        return rows

    def _meet_role_authusers(self, accounts: dict[str, dict[str, Any]], role_account_map: dict[str, str]) -> dict[str, int]:
        resolved: dict[str, int] = {}
        for role in MEET_ROLES:
            account_id = role_account_map.get(role)
            authuser = _coerce_authuser((accounts.get(account_id or "") or {}).get("authuser"))
            if authuser is not None:
                resolved[role] = authuser
        return resolved

    def _meet_bridge_health_for_settings(self) -> dict[str, Any] | None:
        return self._meet_bridge_health()

    def _meet_browser_live_accounts(self) -> list[dict[str, Any]] | None:
        cdp_endpoint = self._meet_browser_cdp_for_profile(self._meet_profile_path())
        if cdp_endpoint is None:
            return None
        return scan_signed_in_sso_accounts(cdp_endpoint, timeout=2.0)

    def _meet_browser_cdp_for_profile(self, profile_path: Path) -> str | None:
        cdp_endpoint = f"http://127.0.0.1:{DEFAULT_POPUP_PORT}"
        if not cdp_alive(cdp_endpoint):
            return None
        live_profile = browser_profile_root(cdp_endpoint)
        if live_profile is None or live_profile.resolve() != profile_path.resolve():
            return None
        return cdp_endpoint

    def list_meet_sso_accounts(self) -> dict[str, Any]:
        health = self._meet_bridge_health_for_settings()
        live_accounts = (health or {}).get("ssoAccounts")
        if live_accounts is None:
            live_accounts = self._meet_browser_live_accounts()
        account_health = health
        if account_health is None and live_accounts is not None:
            account_health = {
                "hostProfile": {"path": str(self._meet_profile_path())},
                "ssoAccounts": live_accounts,
            }
        accounts = self._sso_accounts(account_health)
        signed_in_emails = {
            str(row.get("email") or "").strip().lower()
            for row in live_accounts or []
            if row.get("signedIn") is True and str(row.get("email") or "").strip()
        }
        rows = self._sso_account_rows(accounts)
        for row in rows:
            row["signed_in"] = str(row.get("email") or "").strip().lower() in signed_in_emails
        return {
            "profile_path": str(self._meet_profile_path()),
            "accounts": rows,
            "signed_in_count": len(signed_in_emails),
            "ready_for_meet": len(signed_in_emails) >= 2,
        }

    def get_meet_browser_settings(self) -> dict[str, Any]:
        backend = str(self.meet_browser_settings.get("browser_backend") or "windows")
        profile_path = self._meet_profile_path()
        command = ["ws-collab-meet-bridge", "--profile", str(profile_path), "--browser-backend", backend, "--companion"]
        return {
            "browser_backend": backend,
            "profile_path": str(profile_path),
            "next_launch_command": " ".join(f'"{part}"' if " " in part else part for part in command),
        }

    def set_meet_browser_settings(
        self,
        browser_backend: str,
        profile_path: str,
    ) -> dict[str, Any]:
        backend = str(browser_backend or "windows").strip().lower()
        if backend not in {"windows", "wsl"}:
            raise ValidationError(f"invalid browser backend: {backend!r}")
        path = str(profile_path or "").strip() or str(DEFAULT_PROFILE)
        self.meet_browser_settings.set("browser_backend", backend)
        self.meet_browser_settings.set("profile_path", path)
        return self.get_meet_browser_settings()

    def get_meet_role_assignments(self, meeting_url: str = "") -> dict[str, Any]:
        profile_path = self._meet_profile_path()
        backend = str(self.meet_browser_settings.get("browser_backend") or "windows")
        health = self._meet_bridge_health_for_settings()
        accounts = self._sso_accounts(health, profile_path=profile_path)
        known_meeting_urls = self._known_meeting_urls(profile_path)
        role_account_map, role_overrides = self._effective_meet_role_account_map(
            accounts,
            profile_path,
            meeting_url,
        )
        global_role_account_map = self._meet_role_account_map(accounts, profile_path)
        command = [
            "ws-collab-meet-bridge",
            "--profile",
            str(profile_path),
            "--browser-backend",
            backend,
            "--companion",
        ]
        if meeting_url:
            command.extend(["--meet", self._meet_assignment_key(meeting_url)])
        for role, authuser in self._meet_role_authusers(accounts, role_account_map).items():
            if role in ("host", "companion") or role_account_map.get(role):
                command.extend(["--role-authuser", f"{role}={authuser}"])
                account = accounts.get(role_account_map.get(role, ""), {})
                email = str(account.get("email") or "").strip().lower()
                if email:
                    command.extend(["--role-email", f"{role}={email}"])
        role_assignments = self._meet_role_assignments(accounts, role_account_map)
        companion_click = self._effective_meet_companion_click(profile_path, meeting_url)
        if companion_click.get("enabled"):
            command.append("--companion-click")
            command.extend(["--companion-click-interval", f"{float(companion_click.get('intervalSeconds') or 2.0):g}"])
            command.extend(["--companion-click-mode", str(companion_click.get("mode") or "reactive")])
            command.extend(["--companion-click-trigger", str(companion_click.get("trigger") or "caption")])
            command.extend(["--companion-click-after", f"{float(companion_click.get('afterSeconds') or 10.0):g}"])
            command.extend(["--companion-click-silence-ms", f"{float(companion_click.get('silenceMs') or 500.0):g}"])
            command.extend(["--companion-click-min-gap", f"{float(companion_click.get('minGapSeconds') or 6.0):g}"])
            command.extend(["--companion-click-max-wait", f"{float(companion_click.get('maxWaitSeconds') or 0.0):g}"])
            command.extend(["--companion-click-audio-rms-threshold", f"{float(companion_click.get('audioRmsThreshold') or 0.015):g}"])
            command.extend(["--companion-click-ms", f"{float(companion_click.get('clickMs') or 100.0):g}"])
            command.extend(["--companion-click-gain", f"{float(companion_click.get('gain') or 0.12):g}"])
            command.extend(["--companion-click-sound", str(companion_click.get("sound") or "uh")])
            command.extend(["--companion-click-f0", f"{float(companion_click.get('f0Hz') or 125.0):g}"])
            command.extend(["--companion-click-f1", f"{float(companion_click.get('f1Hz') or 600.0):g}"])
            command.extend(["--companion-click-f2", f"{float(companion_click.get('f2Hz') or 1300.0):g}"])
        stored_meeting_maps = self._meet_profile_state(profile_path).get("meeting_role_account_maps", {})
        meeting_role_account_maps = (
            stored_meeting_maps if isinstance(stored_meeting_maps, dict) else {}
        )
        cdp_endpoint = self._meet_browser_cdp_for_profile(profile_path)
        for assignment in role_assignments:
            account = accounts.get(str(assignment.get("account_id") or ""), {})
            email = str(account.get("email") or "").strip().lower()
            tab = find_sso_tab(cdp_endpoint, email) if cdp_endpoint and email else None
            assignment["tab_exists"] = tab is not None
            assignment["tab"] = tab
        return {
            "accounts": self._sso_account_rows(accounts),
            "scope": "meeting" if meeting_url else "global",
            "meeting_url": self._meet_assignment_key(meeting_url) if meeting_url else "",
            "role_account_map": role_account_map,
            "global_role_account_map": global_role_account_map,
            "role_overrides": role_overrides,
            "meeting_role_account_maps": meeting_role_account_maps,
            "known_meeting_urls": known_meeting_urls,
            "companion_click": companion_click,
            "inherited_roles": [
                role for role in role_account_map
                if meeting_url and role not in role_overrides
            ],
            "role_assignments": role_assignments,
            "role_arguments": " ".join(f'"{part}"' if " " in part else part for part in command),
        }

    def set_meet_role_assignments(
        self,
        role_account_map: dict[str, Any] | None,
        meeting_url: str = "",
    ) -> dict[str, Any]:
        profile_path = self._meet_profile_path()
        sso_state = self.list_meet_sso_accounts()
        accounts = {
            str(row["id"]): row
            for row in sso_state.get("accounts") or []
            if row.get("id")
        }
        supplied = role_account_map or {}
        unknown_roles = sorted(set(supplied) - set(MEET_ROLES))
        if unknown_roles:
            raise ValidationError(f"unknown Meet role: {str(unknown_roles[0])!r}")
        selected: dict[str, str] = {}
        for role_name in MEET_ROLES:
            value = str(supplied.get(role_name) or "").strip()
            if value and value != "__default__" and value not in accounts:
                raise ValidationError(f"unknown SSO account for {role_name}: {value}")
            if value == "__default__" and not meeting_url:
                raise ValidationError("(default) is only valid for a meeting-specific role")
            selected[role_name] = value
        if meeting_url:
            key = self._meet_assignment_key(meeting_url)
            state = self._meet_profile_state(profile_path)
            maps = state.get("meeting_role_account_maps", {})
            maps = dict(maps) if isinstance(maps, dict) else {}
            raw_overrides = maps.get(key, {})
            overrides = {
                role: account_id
                for role, account_id in (raw_overrides.items() if isinstance(raw_overrides, dict) else [])
                if role in MEET_ROLES and account_id in accounts
            }
            for role_name in MEET_ROLES:
                if role_name not in supplied:
                    continue
                account_id = selected[role_name]
                if not account_id or account_id == "__default__":
                    overrides.pop(role_name, None)
                else:
                    overrides[role_name] = account_id
            effective = self._meet_role_account_map(accounts, profile_path)
            effective.update(overrides)
            if overrides:
                maps[key] = overrides
            else:
                maps.pop(key, None)
            self._set_meet_profile_state(
                profile_path,
                accounts=accounts,
                meeting_role_account_maps=maps,
            )
            self._remember_known_meeting_urls(profile_path, {key})
            return self.get_meet_role_assignments(key)
        cleaned = {role: account_id for role, account_id in selected.items() if account_id}
        self._set_meet_profile_state(profile_path, accounts=accounts, role_account_map=cleaned)
        return self.get_meet_role_assignments()

    def clear_meet_role_assignments(self, meeting_url: str) -> dict[str, Any]:
        profile_path = self._meet_profile_path()
        key = self._meet_assignment_key(meeting_url)
        state = self._meet_profile_state(profile_path)
        maps = state.get("meeting_role_account_maps", {})
        maps = dict(maps) if isinstance(maps, dict) else {}
        maps.pop(key, None)
        self._set_meet_profile_state(profile_path, meeting_role_account_maps=maps)
        self._remember_known_meeting_urls(profile_path, {key})
        return self.get_meet_role_assignments(key)

    def get_meet_companion_click(self, meeting_url: str = "") -> dict[str, Any]:
        profile_path = self._meet_profile_path()
        known_meeting_urls = self._known_meeting_urls(profile_path)
        default, overrides = self._meet_companion_click_maps(profile_path)
        effective = self._effective_meet_companion_click(profile_path, meeting_url)
        return {
            **effective,
            "scope": "meeting" if meeting_url else "global",
            "globalDefault": default,
            "meetingOverrides": overrides,
            "knownMeetingUrls": known_meeting_urls,
        }

    def set_meet_companion_click(
        self,
        enabled: bool,
        interval_seconds: float | int | str | None = None,
        meeting_url: str = "",
        *,
        mode: str | None = None,
        trigger: str | None = None,
        after_seconds: float | int | str | None = None,
        silence_ms: float | int | str | None = None,
        min_gap_seconds: float | int | str | None = None,
        max_wait_seconds: float | int | str | None = None,
        audio_rms_threshold: float | int | str | None = None,
        click_ms: float | int | str | None = None,
        gain: float | int | str | None = None,
        sound: str | None = None,
        f0_hz: float | int | str | None = None,
        f1_hz: float | int | str | None = None,
        f2_hz: float | int | str | None = None,
    ) -> dict[str, Any]:
        profile_path = self._meet_profile_path()
        _default, overrides = self._meet_companion_click_maps(profile_path)
        current = self._effective_meet_companion_click(profile_path, meeting_url)
        def positive(value: Any, key: str, label: str) -> float:
            try:
                parsed = float(value if value is not None else current[key])
            except (TypeError, ValueError) as error:
                raise ValidationError(f"{label} must be a positive number") from error
            if parsed <= 0:
                raise ValidationError(f"{label} must be a positive number")
            return parsed

        def nonnegative(value: Any, key: str, label: str) -> float:
            try:
                parsed = float(value if value is not None else current.get(key, 0.0))
            except (TypeError, ValueError) as error:
                raise ValidationError(f"{label} must be a non-negative number") from error
            if parsed < 0:
                raise ValidationError(f"{label} must be a non-negative number")
            return parsed

        parsed_mode = str(mode or current.get("mode") or "reactive").lower()
        if parsed_mode not in {"reactive", "fixed"}:
            raise ValidationError("mode must be 'reactive' or 'fixed'")
        parsed_trigger = str(trigger or current.get("trigger") or "caption").lower()
        if parsed_trigger not in {"caption", "audio", "both"}:
            raise ValidationError("trigger must be 'caption', 'audio', or 'both'")
        parsed_sound = str(sound or current.get("sound") or "uh").lower()
        if parsed_sound not in {"uh", "click"}:
            raise ValidationError("sound must be 'uh' or 'click'")
        setting = {
            "enabled": bool(enabled),
            "intervalSeconds": positive(interval_seconds, "intervalSeconds", "interval_seconds"),
            "mode": parsed_mode,
            "trigger": parsed_trigger,
            "afterSeconds": positive(after_seconds, "afterSeconds", "after_seconds"),
            "silenceMs": positive(silence_ms, "silenceMs", "silence_ms"),
            "minGapSeconds": positive(min_gap_seconds, "minGapSeconds", "min_gap_seconds"),
            "maxWaitSeconds": nonnegative(max_wait_seconds, "maxWaitSeconds", "max_wait_seconds"),
            "audioRmsThreshold": nonnegative(audio_rms_threshold, "audioRmsThreshold", "audio_rms_threshold"),
            "clickMs": positive(click_ms, "clickMs", "click_ms"),
            "gain": min(1.0, positive(gain, "gain", "gain")),
            "sound": parsed_sound,
            "f0Hz": positive(f0_hz, "f0Hz", "f0_hz"),
            "f1Hz": positive(f1_hz, "f1Hz", "f1_hz"),
            "f2Hz": positive(f2_hz, "f2Hz", "f2_hz"),
        }
        if meeting_url:
            key = self._meet_assignment_key(meeting_url)
            overrides = dict(overrides)
            overrides[key] = setting
            self._set_meet_profile_state(profile_path, meeting_companion_click=overrides)
            self._remember_known_meeting_urls(profile_path, {key})
            return self.get_meet_companion_click(key)
        self._set_meet_profile_state(profile_path, companion_click=setting)
        return self.get_meet_companion_click()

    def clear_meet_companion_click(self, meeting_url: str = "") -> dict[str, Any]:
        profile_path = self._meet_profile_path()
        if meeting_url:
            key = self._meet_assignment_key(meeting_url)
            _default, overrides = self._meet_companion_click_maps(profile_path)
            overrides = dict(overrides)
            overrides.pop(key, None)
            self._set_meet_profile_state(profile_path, meeting_companion_click=overrides)
            self._remember_known_meeting_urls(profile_path, {key})
            return self.get_meet_companion_click(key)
        self._set_meet_profile_state(
            profile_path,
            companion_click={
                "enabled": False,
                "intervalSeconds": 2.0,
                "mode": "reactive",
                "trigger": "caption",
                "afterSeconds": 10.0,
                "silenceMs": 500.0,
                "minGapSeconds": 6.0,
                "maxWaitSeconds": 0.0,
                "audioRmsThreshold": 0.015,
                "clickMs": 100.0,
                "gain": 0.12,
                "sound": "uh",
                "f0Hz": 125.0,
                "f1Hz": 600.0,
                "f2Hz": 1300.0,
            },
        )
        return self.get_meet_companion_click()

    def start_meet_bridge(self, meeting_url: str = "", *, new: bool = False) -> dict[str, Any]:
        health = self._meet_bridge_health(timeout=0.75)
        process_running = self._meet_bridge_tracked_process_running()
        pid_running = None if health or process_running else self._meet_bridge_pid_running()
        port_open = False if health or process_running or pid_running else self._meet_bridge_port_open()
        if health or process_running or pid_running or port_open:
            return {
                "ok": True,
                "started": False,
                "already_running": True,
                "meeting_url": (health or {}).get("meetingUrl"),
                "pid": pid_running,
            }
        target = self._meet_assignment_key(meeting_url) if meeting_url else ""
        settings = self.get_meet_role_assignments(target)
        assignments = {
            str(row.get("role") or ""): row
            for row in settings.get("role_assignments") or []
        }
        missing = [
            role.upper()
            for role in ("host", "companion")
            if not assignments.get(role, {}).get("account_id")
        ]
        unresolved = [
            role.upper()
            for role in ("host", "companion")
            if assignments.get(role, {}).get("account_id")
            and (
                assignments.get(role, {}).get("authuser") is None
                or not assignments.get(role, {}).get("email")
            )
        ]
        if missing:
            raise ValidationError(f"assign an SSO account for: {', '.join(missing)}")
        if unresolved:
            raise ValidationError(
                f"scan the signed-in accounts before starting; unresolved roles: {', '.join(unresolved)}"
            )
        argv = [
            sys.executable,
            "-u",
            "-m",
            "ws_collab.meet_bridge",
            "--profile",
            str(self._meet_profile_path()),
            "--browser-backend",
            str(self.meet_browser_settings.get("browser_backend") or "windows"),
            "--companion",
        ]
        if target:
            argv.extend(["--meet", target])
        elif new:
            argv.append("--new")
        click = self.get_meet_companion_click(target)
        if click.get("enabled"):
            argv.append("--companion-click")
            argv.extend(["--companion-click-interval", f"{float(click.get('intervalSeconds') or 2.0):g}"])
            argv.extend(["--companion-click-mode", str(click.get("mode") or "reactive")])
            argv.extend(["--companion-click-trigger", str(click.get("trigger") or "caption")])
            argv.extend(["--companion-click-after", f"{float(click.get('afterSeconds') or 10.0):g}"])
            argv.extend(["--companion-click-silence-ms", f"{float(click.get('silenceMs') or 500.0):g}"])
            argv.extend(["--companion-click-min-gap", f"{float(click.get('minGapSeconds') or 6.0):g}"])
            argv.extend(["--companion-click-max-wait", f"{float(click.get('maxWaitSeconds') or 0.0):g}"])
            argv.extend(["--companion-click-audio-rms-threshold", f"{float(click.get('audioRmsThreshold') or 0.015):g}"])
            argv.extend(["--companion-click-ms", f"{float(click.get('clickMs') or 100.0):g}"])
            argv.extend(["--companion-click-gain", f"{float(click.get('gain') or 0.12):g}"])
            argv.extend(["--companion-click-sound", str(click.get("sound") or "uh")])
            argv.extend(["--companion-click-f0", f"{float(click.get('f0Hz') or 125.0):g}"])
            argv.extend(["--companion-click-f1", f"{float(click.get('f1Hz') or 600.0):g}"])
            argv.extend(["--companion-click-f2", f"{float(click.get('f2Hz') or 1300.0):g}"])
        for role in ("host", "companion"):
            assignment = assignments[role]
            argv.extend([
                "--role-authuser",
                f"{role}={int(assignment['authuser'])}",
                "--role-email",
                f"{role}={str(assignment['email']).strip().lower()}",
            ])
        log_path = Path(self.config.state_dir) / "meet_bridge.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        process_env = os.environ.copy()
        process_env["PYTHONUNBUFFERED"] = "1"
        if not self.config.auth_disabled:
            token = ""
            for preferred_role in ("admin", "operator", "worker"):
                token = next(
                    (
                        value
                        for value, descriptor in self.config.tokens.items()
                        if descriptor.get("role") == preferred_role
                    ),
                    "",
                )
                if token:
                    break
            if token:
                process_env["WS_COLLAB_TOKEN"] = token
        with log_path.open("ab") as log:
            self._meet_bridge_process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                env=process_env,
            )
        self._write_meet_bridge_pid(self._meet_bridge_process.pid)
        return {
            "ok": True,
            "started": True,
            "already_running": False,
            "pid": self._meet_bridge_process.pid,
            "meeting_url": target or None,
            "new": bool(new and not target),
            "log_path": str(log_path),
        }

    def _meet_sso_in_use_warning(self, path: Path) -> str | None:
        health = self._meet_bridge_health()
        if not health:
            return "bridge not reachable to confirm whether this profile is already in use"
        for proc in health.get("processes") or []:
            if str(proc.get("profile") or "") != str(path) or proc.get("alive") is not True:
                continue
            return f"Meet browser profile appears to be in use by the running meet bridge process (pid {proc.get('pid')})"
        return None

    def _meet_bridge_command(self, command: str, timeout: float = 1.0) -> dict[str, Any] | None:
        import urllib.request

        payload = json.dumps({"command": command}).encode("utf-8")
        request = urllib.request.Request(
            "http://127.0.0.1:48699/command",
            data=payload,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            return None

    def meet_bridge_health(self) -> dict[str, Any]:
        health = self._meet_bridge_health(timeout=15.0)
        if health is None:
            raise NotFoundError("Meet bridge worker is offline")
        return health

    def meet_bridge_captions(self, since: float | str = 0.0, from_end: int | str | None = None) -> dict[str, Any]:
        import urllib.parse
        import urllib.request

        try:
            timestamp = float(since)
        except (TypeError, ValueError) as error:
            raise ValidationError("since must be a number") from error

        params = {"since": timestamp}
        if from_end is not None:
            try:
                params["fromEnd"] = int(from_end)
            except (TypeError, ValueError) as error:
                raise ValidationError("fromEnd must be an integer") from error

        query = urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:48699/captions?{query}",
                timeout=2.0,
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as error:
            raise NotFoundError("Meet bridge worker is offline") from error

    def meet_bridge_command(self, command: str) -> dict[str, Any]:
        value = str(command or "").strip()
        if not value:
            raise ValidationError("command is required")
        result = self._meet_bridge_command(value, timeout=2.0)
        if result is not None:
            return result
        join = re.fullmatch(r"/join\s+(.+)", value)
        if join:
            started = self.start_meet_bridge(join.group(1).strip())
        elif value == "/new":
            started = self.start_meet_bridge(new=True)
        else:
            raise NotFoundError("Meet bridge worker is offline")
        verdict = "bridge already running" if started.get("already_running") else "bridge starting"
        if started.get("pid"):
            verdict += f" (pid {started['pid']})"
        return {**started, "verdict": verdict}

    def _meet_bridge_can_reuse_sso(self, health: dict[str, Any] | None, path: Path) -> bool:
        if not health:
            return False
        for proc in health.get("processes") or []:
            if str(proc.get("profile") or "") != str(path) or proc.get("alive") is not True:
                continue
            return True
        return False

    def _sso_target(self, account_id: str = "", add_account: bool = False, health: dict[str, Any] | None = None) -> tuple[str, int | None]:
        if add_account:
            return "https://accounts.google.com/AccountChooser?continue=https://accounts.google.com/", None
        accounts = self._sso_accounts(health, profile_path=self._meet_profile_path())
        account = accounts.get(account_id)
        if account is None:
            raise ValidationError(f"unknown SSO account: {account_id}")
        authuser = _coerce_authuser(account.get("authuser"))
        target = "https://accounts.google.com/" if authuser is None else f"https://accounts.google.com/?authuser={authuser}"
        return target, authuser

    def open_meet_sso_account(self, account_id: str = "", add_account: bool = False) -> dict[str, Any]:
        account_id = str(account_id or "").strip()
        if not add_account and not account_id:
            raise ValidationError("expected account_id or add_account=true")
        path = self._meet_profile_path()
        path.mkdir(parents=True, exist_ok=True)
        health = self._meet_bridge_health()
        warning = self._meet_sso_in_use_warning(path)
        target_url, authuser = self._sso_target(account_id, add_account, health)
        reuse_command = "/sso add-account" if add_account else f"/sso {authuser}"
        if (add_account or authuser is not None) and self._meet_bridge_can_reuse_sso(health, path):
            result = self._meet_bridge_command(reuse_command)
            verdict = str((result or {}).get("verdict") or "")
            if result and result.get("ok") and verdict.startswith("sso:"):
                return {"ok": True, "account_id": account_id or None, "path": str(path), "reused_bridge_window": True, "warning": warning}
        cdp_endpoint = self._meet_browser_cdp_for_profile(path)
        if cdp_endpoint is not None:
            if add_account:
                existing = find_add_account_tab(cdp_endpoint)
            else:
                accounts = self._sso_accounts(health, profile_path=path)
                email = str((accounts.get(account_id) or {}).get("email") or "")
                existing = find_sso_connector_tab(cdp_endpoint, email) if email else None
            info, _ = reuse_or_open_tab(
                cdp_endpoint,
                target_url,
                existing_in_scope=existing,
                navigate_existing=bool(add_account and existing),
            )
            if info and info.get("webSocketDebuggerUrl"):
                return {
                    "ok": True,
                    "account_id": account_id or None,
                    "path": str(path),
                    "reused_bridge_window": True,
                    "warning": warning,
                }
            raise ValidationError("could not open or reuse the SSO tab in the existing browser window")
        argv = [
            find_browser(None),
            f"--remote-debugging-port={DEFAULT_POPUP_PORT}",
            f"--user-data-dir={path}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            target_url,
        ]
        process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "account_id": account_id or None, "path": str(path), "pid": process.pid, "reused_bridge_window": False, "warning": warning}

    def foreground_meet_sso_account(self, account_id: str) -> dict[str, Any]:
        account_id = str(account_id or "").strip()
        profile_path = self._meet_profile_path()
        accounts = self._sso_accounts(self._meet_bridge_health_for_settings(), profile_path=profile_path)
        account = accounts.get(account_id)
        if account is None:
            raise ValidationError(f"unknown SSO account: {account_id}")
        email = str(account.get("email") or "").strip().lower()
        if not email:
            return {
                "ok": False,
                "account_id": account_id,
                "tab_exists": False,
                "verdict": f"no email is known for {account_id}",
            }
        cdp_endpoint = self._meet_browser_cdp_for_profile(profile_path)
        if cdp_endpoint is None:
            return {
                "ok": False,
                "account_id": account_id,
                "email": email,
                "tab_exists": False,
                "verdict": "the configured browser profile is not open",
            }
        tab = foreground_sso_tab(cdp_endpoint, email)
        if tab is None:
            return {
                "ok": False,
                "account_id": account_id,
                "email": email,
                "tab_exists": False,
                "verdict": f"no existing browser page was found for {email}",
            }
        return {
            "ok": True,
            "account_id": account_id,
            "email": email,
            "tab_exists": True,
            "tab": tab,
            "verdict": f"foregrounded existing browser page for {email}",
        }

    def forget_meet_sso_profile(self) -> dict[str, Any]:
        path = self._meet_profile_path()
        warning = self._meet_sso_in_use_warning(path)
        if warning and "in use" in warning:
            raise ConflictError("Close the bridge browser window first; that profile appears to be in use by the running meet bridge process.")
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        self.meet_browser_settings.clear_profile_state(path)
        return {"ok": True, "path": str(path), "deleted": True, "warning": warning}

    # ------------------------------------------- per-field value cache (candidates)
    _FIELD_CACHE_SKIP = {"id", "raw", "text", "timestamp", "ts", "seq", "mailboxId", "forwarded_by"}
    _FIELD_CACHE_MAX = 16
    # Hard bounds that keep the cache from growing without limit no matter what
    # records are observed (a previous incident nested the cache's own on-disk
    # document into itself until the file reached gigabytes).
    _FIELD_CACHE_MAX_FIELDS_PER_STREAM = 512
    _FIELD_CACHE_MAX_FIELD_DEPTH = 6
    _FIELD_CACHE_MAX_VALUE_CHARS = 256
    _FIELD_OBSERVATIONS = {
        "chat_bubble": "Fields observed in chat message records.",
        "mailbox_definition": "Fields observed in mailbox JSON definitions.",
    }

    def _cache_config_path(self) -> Path:
        return Path(self.store.directory) / "field_cache_config.json"

    def _cache_data_path(self) -> Path:
        return Path(self.store.directory) / "fields_seen_in_streams.json"

    def _load_field_cache(self) -> None:
        import json

        # --- config: layered per-field cache-limit overrides ------------------
        try:
            cfg = json.loads(self._cache_config_path().read_text("utf-8"))
        except Exception:
            cfg = {}
        if isinstance(cfg, dict):
            glob = cfg.get("cache-overrides")
            if isinstance(glob, dict):
                for field, lim in glob.items():
                    if isinstance(field, str) and isinstance(lim, (int, float)) and int(lim) > 0:
                        self._field_overrides_global[field] = int(lim)
            streams_cfg = cfg.get("streams")
            if isinstance(streams_cfg, dict):
                for stream, over in streams_cfg.items():
                    if isinstance(stream, str) and isinstance(over, dict):
                        self._field_overrides_stream[stream] = {
                            str(f): int(v) for f, v in over.items() if isinstance(v, (int, float)) and int(v) > 0
                        }
            observations_cfg = cfg.get("observations")
            if isinstance(observations_cfg, dict):
                for observation, observation_cfg in observations_cfg.items():
                    if not isinstance(observation, str) or not isinstance(observation_cfg, dict):
                        continue
                    field_overrides = observation_cfg.get("cache-overrides")
                    if isinstance(field_overrides, dict):
                        self._field_overrides_observation[observation] = {
                            str(field): int(limit)
                            for field, limit in field_overrides.items()
                            if isinstance(limit, (int, float)) and int(limit) > 0
                        }
                    stream_overrides = observation_cfg.get("streams")
                    if isinstance(stream_overrides, dict):
                        self._field_overrides_observation_stream[observation] = {
                            str(stream): {
                                str(field): int(limit)
                                for field, limit in overrides.items()
                                if isinstance(limit, (int, float)) and int(limit) > 0
                            }
                            for stream, overrides in stream_overrides.items()
                            if isinstance(overrides, dict)
                        }

        # --- data: cached values + inferred types ----------------------------
        try:
            data_path = self._cache_data_path()
            # Self-heal: a bounded cache stays tiny; a huge file means a past
            # runaway (e.g. the cache observing its own document). Set it aside
            # instead of spending minutes/gigabytes parsing it.
            try:
                if data_path.stat().st_size > 64 * 1024 * 1024:
                    quarantine = data_path.with_name(data_path.name + ".oversized")
                    quarantine.unlink(missing_ok=True)
                    data_path.rename(quarantine)
                    self._warnings.append(
                        f"field cache {data_path.name} was oversized and has been reset; old file kept as {quarantine.name}"
                    )
            except OSError:
                pass
            data = json.loads(data_path.read_text("utf-8"))
        except Exception:
            data = {}
        observations = data.get("observations") if isinstance(data, dict) else None
        if isinstance(observations, dict):
            scoped_data = {
                observation: (
                    entry.get("streams")
                    if isinstance(entry, dict) and isinstance(entry.get("streams"), dict)
                    else {}
                )
                for observation, entry in observations.items()
            }
        else:
            # Version 1 stored chat-message observations directly by stream.
            scoped_data = {"chat_bubble": data if isinstance(data, dict) else {}}
        for observation, streams in scoped_data.items():
            cache, type_cache = self._observation_caches(observation)
            for stream, entry in streams.items():
                if not isinstance(stream, str) or not isinstance(entry, dict):
                    continue
                out: dict[str, list[str]] = {}
                out_types: dict[str, str] = {}
                fields = entry.get("fields") if isinstance(entry.get("fields"), dict) else {}
                for field, decl in fields.items():
                    if not isinstance(field, str) or not isinstance(decl, dict):
                        continue
                    if field.count(".") >= self._FIELD_CACHE_MAX_FIELD_DEPTH:
                        continue
                    if len(out) >= self._FIELD_CACHE_MAX_FIELDS_PER_STREAM:
                        break
                    values = decl.get("values")
                    if not isinstance(values, list):
                        continue
                    out_types[field] = str(decl.get("type") or "string")
                    out[field] = [
                        str(v) for v in values if len(str(v)) <= self._FIELD_CACHE_MAX_VALUE_CHARS
                    ][-self._field_limit(stream, field, observation):]
                cache[stream] = out
                type_cache[stream] = out_types

    def _observation_caches(
        self,
        observation: str,
    ) -> tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, str]]]:
        if observation == "mailbox_definition":
            return self._definition_field_cache, self._definition_field_types
        return self._field_cache, self._field_types

    def _field_limit(self, stream: str, field: str, observation: str = "chat_bubble") -> int:
        """Effective limit: observed-stream > observed-field > global stream/field > default."""
        observed_stream = self._field_overrides_observation_stream.get(observation, {}).get(stream, {})
        if field in observed_stream:
            return observed_stream[field]
        observed_fields = self._field_overrides_observation.get(observation, {})
        if field in observed_fields:
            return observed_fields[field]
        s = self._field_overrides_stream.get(stream, {})
        if field in s:
            return s[field]
        if field in self._field_overrides_global:
            return self._field_overrides_global[field]
        return self._FIELD_CACHE_MAX

    def _cache_config_doc(self) -> dict[str, Any]:
        # cache_config.json: default + layered per-field limit overrides (editable).
        cfg: dict[str, Any] = {"default_limit": self._FIELD_CACHE_MAX}
        if self._field_overrides_global:
            cfg["cache-overrides"] = dict(self._field_overrides_global)
        streams = {s: dict(o) for s, o in self._field_overrides_stream.items() if o}
        if streams:
            cfg["streams"] = streams
        cfg["observations"] = {
            observation: {
                "description": description,
                **(
                    {"cache-overrides": dict(self._field_overrides_observation[observation])}
                    if self._field_overrides_observation.get(observation)
                    else {}
                ),
                **(
                    {"streams": {
                        stream: dict(overrides)
                        for stream, overrides in self._field_overrides_observation_stream[observation].items()
                        if overrides
                    }}
                    if self._field_overrides_observation_stream.get(observation)
                    else {}
                ),
            }
            for observation, description in self._FIELD_OBSERVATIONS.items()
        }
        return cfg

    def _cache_data_doc(self) -> dict[str, Any]:
        # Cache data is separated by what was observed; message fields and
        # mailbox-definition fields serve different UI decisions.
        def streams_doc(observation: str) -> dict[str, Any]:
            cache, types = self._observation_caches(observation)
            return {
                stream: {
                    "cached_limit": self._FIELD_CACHE_MAX,
                    "fields": {
                        field: {
                            "type": types.get(stream, {}).get(field, "string"),
                            "cached_limit": self._field_limit(stream, field, observation),
                            "values": values,
                        }
                        for field, values in fields.items()
                    },
                }
                for stream, fields in cache.items()
            }

        return {
            "schema_version": 2,
            "observations": {
                observation: {
                    "description": description,
                    "streams": streams_doc(observation),
                }
                for observation, description in self._FIELD_OBSERVATIONS.items()
            },
        }

    def _save_cache_config(self) -> None:
        import json
        import os

        path = self._cache_config_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._cache_config_doc(), indent=2), "utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _save_field_cache(self, *, force: bool = False) -> None:
        import json
        import os

        if not self._field_cache_dirty:
            return
        now = time.time()
        if not force and (now - self._field_cache_saved_at) < 2.0:
            return
        path = self._cache_data_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._cache_data_doc(), indent=2), "utf-8")
            os.replace(tmp, path)
            self._field_cache_dirty = False
            self._field_cache_saved_at = now
        except OSError:
            pass

    def _is_internal_cache_source(self, source: str) -> bool:
        """True when a virtual source reads one of this service's own cache
        files -- observing those would feed the cache back into itself (the
        exact loop that once grew the cache file to gigabytes)."""
        if source.startswith("disk:"):
            path_str = source[len("disk:"):]
        elif source.startswith("file:"):
            path_str = source[len("file:"):]
        elif source.endswith(".json"):
            path_str = source
        else:
            return False
        path = Path(path_str)
        if not path.is_absolute():
            path = Path(self.store.directory) / path
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return resolved in {self._cache_data_path().resolve(), self._cache_config_path().resolve()}

    def _remember_field_values(
        self,
        stream: str,
        records: list[dict[str, Any]],
        *,
        observation: str = "chat_bubble",
    ) -> None:
        """Record primitive fields in the schema scope that observed them."""
        if not stream or not records:
            return
        spec = self._virtual.get(stream)
        if spec and self._is_internal_cache_source(str(spec.get("source", ""))):
            return
        cache, type_cache = self._observation_caches(observation)
        bucket = cache.setdefault(stream, {})
        types = type_cache.setdefault(stream, {})

        def vtype(v: Any) -> str:
            if isinstance(v, bool):
                return "boolean"
            if isinstance(v, (int, float)):
                return "number"
            return "string"

        def offer(field: str, value: Any) -> None:
            if field.split(".")[-1] in self._FIELD_CACHE_SKIP or value is None or isinstance(value, (dict, list)):
                return
            if field.count(".") >= self._FIELD_CACHE_MAX_FIELD_DEPTH:
                return
            sval = str(value)
            if sval == "" or len(sval) > self._FIELD_CACHE_MAX_VALUE_CHARS:
                return
            if field not in bucket and len(bucket) >= self._FIELD_CACHE_MAX_FIELDS_PER_STREAM:
                # Bounded: evict the oldest-known field so recent schema wins.
                bucket.pop(next(iter(bucket)), None)
            t = vtype(value)
            prior = types.get(field)
            if prior is None:
                types[field] = t
            elif prior != t and prior != "mixed":
                types[field] = "mixed"
            seq = bucket.setdefault(field, [])
            if seq and seq[-1] == sval:
                return
            if sval in seq:
                seq.remove(sval)
            seq.append(sval)
            lim = self._field_limit(stream, field, observation)
            if len(seq) > lim:
                del seq[: len(seq) - lim]
            self._field_cache_dirty = True

        def observe(prefix: str, value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    observe(f"{prefix}.{key}" if prefix else str(key), child)
            else:
                offer(prefix, value)

        for item in records:
            rec = item if isinstance(item, dict) else {}
            for key, value in rec.items():
                if key == "raw" and isinstance(value, dict):
                    for raw_key, raw_value in value.items():
                        observe(str(raw_key), raw_value)
                else:
                    observe(str(key), value)

    def field_values(self, mailbox: str, *, observation: str = "chat_bubble") -> dict[str, Any]:
        """Candidate field values (and inferred types) for a mailbox, aggregated
        across a merge's members, within one observation scope."""
        if observation not in self._FIELD_OBSERVATIONS:
            raise ValidationError(f"unknown field observation: {observation}")
        cache, type_cache = self._observation_caches(observation)
        streams: list[str] = []
        if observation == "mailbox_definition" and mailbox in ("*", "all"):
            streams = sorted(cache)
        spec = self._virtual.get(mailbox) if observation == "chat_bubble" else None
        if streams:
            pass
        elif spec:
            source = str(spec.get("source", ""))
            if source.startswith("merge:"):
                target = source[len("merge:"):].strip()
                if target in ("*", "all"):
                    streams = [s for s in STREAMS] + list(self._dynamic_mailboxes)
                else:
                    streams = [n.strip() for n in target.split(",") if n.strip()]
            else:
                streams = [mailbox]
        else:
            streams = [mailbox]
        merged: dict[str, list[str]] = {}
        types: dict[str, str] = {}
        for s in streams:
            for field, values in cache.get(s, {}).items():
                dest = merged.setdefault(field, [])
                for v in values:
                    if v in dest:
                        dest.remove(v)
                    dest.append(v)
                if len(dest) > self._FIELD_CACHE_MAX:
                    del dest[: len(dest) - self._FIELD_CACHE_MAX]
            for field, t in type_cache.get(s, {}).items():
                prior = types.get(field)
                types[field] = t if prior is None else (prior if prior == t else "mixed")
        limits: dict[str, int] = {}
        for s in streams:
            for field in cache.get(s, {}):
                limits[field] = max(limits.get(field, 0), self._field_limit(s, field, observation))
        fields = {
            field: {"type": types.get(field, "string"), "cached_limit": limits.get(field, self._FIELD_CACHE_MAX), "values": values}
            for field, values in merged.items()
        }
        stream_fields = {
            stream: {
                "fields": {
                    field: {
                        "type": type_cache.get(stream, {}).get(field, "string"),
                        "cached_limit": self._field_limit(stream, field, observation),
                        "values": values,
                    }
                    for field, values in cache.get(stream, {}).items()
                }
            }
            for stream in streams
        }
        return {
            "mailbox": mailbox,
            "observation": observation,
            "cached_limit": self._FIELD_CACHE_MAX,
            "fields": fields,
            "streams": stream_fields,
        }

    def set_field_cache_limit(
        self,
        field: str,
        limit: int,
        *,
        stream: str = "",
        observation: str = "",
    ) -> dict[str, Any]:
        """Set a per-field cache limit. With ``stream`` it is a per-(stream,field)
        override; without, it is a global by-field override ("cache-overrides")."""
        field = str(field or "").strip()
        if not field:
            raise ValidationError("field is required")
        lim = int(limit)
        if lim <= 0:
            raise ValidationError("limit must be a positive integer")
        if observation and observation not in self._FIELD_OBSERVATIONS:
            raise ValidationError(f"unknown field observation: {observation}")
        if observation and stream:
            self._field_overrides_observation_stream.setdefault(observation, {}).setdefault(stream, {})[field] = lim
        elif observation:
            self._field_overrides_observation.setdefault(observation, {})[field] = lim
        elif stream:
            self._field_overrides_stream.setdefault(stream, {})[field] = lim
        else:
            self._field_overrides_global[field] = lim
        # Re-trim any affected cached lists to the new effective limit.
        observations = [observation] if observation else list(self._FIELD_OBSERVATIONS)
        for observed in observations:
            cache, _types = self._observation_caches(observed)
            for s, fields in cache.items():
                if stream and s != stream:
                    continue
                seq = fields.get(field)
                if seq is not None:
                    eff = self._field_limit(s, field, observed)
                    if len(seq) > eff:
                        del seq[: len(seq) - eff]
        self._field_cache_dirty = True
        self._save_cache_config()
        self._save_field_cache(force=True)
        return {
            "observation": observation or "*",
            "stream": stream or "*",
            "field": field,
            "cached_limit": lim,
        }

    def get_cache_config_file(self) -> dict[str, Any]:
        """Raw contents of the editable field-cache CONFIG file (limit overrides)."""
        path = self._cache_config_path()
        try:
            content = path.read_text("utf-8")
        except Exception:
            import json

            content = json.dumps(self._cache_config_doc(), indent=2)
        return {"path": str(path), "content": content}

    def get_cache_data_file(self) -> dict[str, Any]:
        """Raw contents of the auto-generated field-cache DATA file (seen values)."""
        path = self._cache_data_path()
        try:
            content = path.read_text("utf-8")
        except Exception:
            content = "{}"
        return {"path": str(path), "content": content}

    def set_cache_config_file(self, content: str) -> dict[str, Any]:
        """Overwrite the field-cache CONFIG file with edited JSON, then reload it and
        re-trim the cached data to the new limits."""
        import json
        import os

        try:
            parsed = json.loads(content)
        except Exception as error:
            raise ValidationError(f"invalid JSON: {error}")
        if not isinstance(parsed, dict):
            raise ValidationError("the cache config must be a JSON object")
        path = self._cache_config_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(parsed, indent=2), "utf-8")
            os.replace(tmp, path)
        except OSError as error:
            raise ValidationError(f"could not write file: {error}")
        # Reload overrides and re-trim existing values to the new effective limits.
        self._field_overrides_global.clear()
        self._field_overrides_stream.clear()
        self._field_overrides_observation.clear()
        self._field_overrides_observation_stream.clear()
        glob = parsed.get("cache-overrides")
        if isinstance(glob, dict):
            for f, lim in glob.items():
                if isinstance(f, str) and isinstance(lim, (int, float)) and int(lim) > 0:
                    self._field_overrides_global[f] = int(lim)
        streams_cfg = parsed.get("streams")
        if isinstance(streams_cfg, dict):
            for s, over in streams_cfg.items():
                if isinstance(s, str) and isinstance(over, dict):
                    self._field_overrides_stream[s] = {
                        str(f): int(v) for f, v in over.items() if isinstance(v, (int, float)) and int(v) > 0
                    }
        observations_cfg = parsed.get("observations")
        if isinstance(observations_cfg, dict):
            for observation, observation_cfg in observations_cfg.items():
                if not isinstance(observation, str) or not isinstance(observation_cfg, dict):
                    continue
                field_overrides = observation_cfg.get("cache-overrides")
                if isinstance(field_overrides, dict):
                    self._field_overrides_observation[observation] = {
                        str(field): int(limit)
                        for field, limit in field_overrides.items()
                        if isinstance(limit, (int, float)) and int(limit) > 0
                    }
                stream_overrides = observation_cfg.get("streams")
                if isinstance(stream_overrides, dict):
                    self._field_overrides_observation_stream[observation] = {
                        str(stream): {
                            str(field): int(limit)
                            for field, limit in overrides.items()
                            if isinstance(limit, (int, float)) and int(limit) > 0
                        }
                        for stream, overrides in stream_overrides.items()
                        if isinstance(overrides, dict)
                    }
        for observation in self._FIELD_OBSERVATIONS:
            cache, _types = self._observation_caches(observation)
            for stream, fields in cache.items():
                for field, seq in fields.items():
                    effective = self._field_limit(stream, field, observation)
                    if len(seq) > effective:
                        del seq[: len(seq) - effective]
        self._field_cache_dirty = True
        self._save_field_cache(force=True)
        return {"ok": True, "path": str(path)}

    # ------------------------------------------------------------------ core io
    def publish(
        self,
        *,
        stream: str,
        type: str,
        data: dict[str, Any] | None = None,
        source_id: str = "system",
        source_kind: str = "system",
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        event = validate_new_event(
            stream,
            type,
            data or {},
            source_id=source_id,
            source_kind=source_kind,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        result = self.store.append(event, on_write=self.broker.publish)
        return {
            "id": result.event.id,
            "seq": result.event.seq,
            "cursor": result.cursor,
            "duplicate": result.duplicate,
            "stream": stream,
            "event_type": type,
            "server_time": utc_now_iso(),
        }

    def read_events(
        self,
        stream: str,
        *,
        after: str | None = None,
        limit: int = 100,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if stream not in STREAMS:
            raise ValidationError(f"unknown stream: {stream!r}", details={"allowed": sorted(STREAMS)})
        predicate = _build_predicate(filters)
        page = self.store.read(stream, after, max(1, min(limit, 1000)), predicate)
        return {
            "stream": stream,
            "events": [event.to_dict() for event in page.events],
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
            "server_time": page.server_time,
            "malformed": page.malformed,
        }

    async def read_events_wait(
        self,
        stream: str,
        *,
        after: str | None = None,
        limit: int = 100,
        filters: dict[str, Any] | None = None,
        wait_ms: int = 0,
    ) -> dict[str, Any]:
        """Cursor read with bounded long-polling (no tight client loop needed)."""

        page = self.read_events(stream, after=after, limit=limit, filters=filters)
        if page["events"] or wait_ms <= 0:
            return page
        loop = asyncio.get_running_loop()
        predicate = _build_predicate(filters)
        sub = self.broker.subscribe({stream}, loop, predicate)
        try:
            await asyncio.wait_for(sub.queue.get(), timeout=wait_ms / 1000.0)
        except asyncio.TimeoutError:
            return page
        finally:
            self.broker.unsubscribe(sub.id)
        return self.read_events(stream, after=after, limit=limit, filters=filters)

    def tail(self, stream: str, count: int = 50, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        if stream not in STREAMS:
            raise ValidationError(f"unknown stream: {stream!r}", details={"allowed": sorted(STREAMS)})
        predicate = _build_predicate(filters)
        events = self.store.tail(stream, max(1, min(count, 2000)), predicate)
        return {"stream": stream, "events": [event.to_dict() for event in events], "server_time": utc_now_iso()}

    # ------------------------------------------------------------- capabilities
    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "server_time": utc_now_iso(),
            "uptime_seconds": round(time.time() - self.started_at, 1),
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "boot_id": self.boot_id,
            "product": "WS_COLLAB",
            "transports": ["http", "https", "ws", "wss"],
            "rest_base": "/ws_collab",
            "versioned_base": "/ws_collab/v1",
            "streams": STREAMS,
            "mailboxes": STREAMS,
            "stream_roles": STREAM_ROLES,
            "auth_methods": ["bearer_token", "session_cookie"],
            "features": {
                "cursor_pagination": True,
                "idempotent_writes": True,
                "long_polling": True,
                "conditional_requests": True,
                "websocket_resume": True,
                "three_stt_engines": len(self.stt_engines),
                "disambiguator": getattr(self.disambiguator, "method_name", "deterministic"),
                "echo_policy": self.config.echo_policy,
                "voice_policy": self.config.tts_policy,
            },
            "warnings": self._warnings,
        }

    # ------------------------------------------------------------- conversation
    def add_conversation(
        self,
        text: str,
        *,
        source_id: str,
        source_kind: str,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("conversation 'text' is required")
        payload = {"text": text, **(data or {})}
        return self.publish(
            stream=STREAM_CONVERSATION,
            type=CONVERSATION_MESSAGE,
            data=payload,
            source_id=source_id,
            source_kind=source_kind,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )

    # ---------------------------------------------------- mailboxes (streams)
    # The mailbox-admin chat UI treats every durable JSONL stream as a
    # "mailbox": the stream file is the mailbox and its events are the messages.
    # Exposing streams under a mailbox-shaped API lets the shared workbench
    # ChatConversation browse ws_collab streams with no separate backend.
    @staticmethod
    def _event_to_message(event: dict[str, Any]) -> dict[str, Any]:
        """Map a stream event to the ChatMessage shape ChatConversation reads."""
        data = event.get("data") or {}
        text = ""
        for key in ("text", "message", "content", "summary", "utterance"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
        return {
            "id": event.get("id"),
            "timestamp": event.get("ts"),
            "from": event.get("source_id"),
            "to": data.get("to") or data.get("target") or event.get("stream"),
            "send_to": data.get("send_to"),
            "text": text,
            "type": event.get("type"),
            "mailboxId": event.get("stream"),
            "author": event.get("source_id"),
            "authorName": data.get("author_name") or event.get("source_id"),
            "mailboxName": event.get("stream"),
            "raw": event,
        }

    # -------------------------------------------- dynamic mailbox registry
    def _mailbox_registry_path(self) -> Path:
        return Path(self.store.directory) / "mailboxes.json"

    def _load_mailbox_registry(self) -> None:
        import json

        try:
            raw = json.loads(self._mailbox_registry_path().read_text("utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        for name, meta in raw.items():
            if not isinstance(name, str) or name in STREAMS:
                continue
            self._dynamic_mailboxes[name] = meta if isinstance(meta, dict) else {}
            self.store.register_mailbox(name)
            DYNAMIC_STREAMS.add(name)

    def _save_mailbox_registry(self) -> None:
        import json
        import os

        path = self._mailbox_registry_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._dynamic_mailboxes, indent=2), "utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    # ------------------------------------------ virtual mailbox registry (durable)
    def _virtual_registry_path(self) -> Path:
        return Path(self.store.directory) / "virtual_mailboxes.json"

    def _load_virtual_registry(self) -> None:
        """Rehydrate runtime-created virtual mailboxes (e.g. saved merge combos)."""
        import json

        try:
            raw = json.loads(self._virtual_registry_path().read_text("utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        for name, spec in raw.items():
            if not isinstance(name, str) or name in STREAMS or not isinstance(spec, dict):
                continue
            if not spec.get("source"):
                continue
            entry: dict[str, Any] = {}
            for k, v in spec.items():
                key = str(k)
                if key == "rules" and isinstance(v, list):
                    entry[key] = [r for r in v if isinstance(r, dict)]
                else:
                    entry[key] = v if isinstance(v, (str, int, float, bool)) else str(v)
            entry["runtime"] = "1"
            self._virtual[name] = entry

    def _save_virtual_registry(self) -> None:
        """Persist only runtime-created virtual mailboxes; config ones stay in config."""
        import json
        import os

        runtime = {n: s for n, s in self._virtual.items() if s.get("runtime") == "1"}
        path = self._virtual_registry_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(runtime, indent=2), "utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _known_mailbox(self, name: str) -> bool:
        return name in STREAMS or name in self._dynamic_mailboxes or name in self._virtual

    def _stream_origin(self, name: str) -> str:
        """Which server a stream logically belongs to, for UI disambiguation.
        Streams named ``workbench_*`` are the workbench server's own streams;
        everything else is a native ws_collab stream."""
        return "workbench" if str(name).startswith("workbench_") else "ws_collab"

    def _writable_mailbox(self, name: str) -> bool:
        if name in self._virtual:
            return False
        if name in self._dynamic_mailboxes:
            return self._dynamic_mailboxes[name].get("writable", True) is not False
        return name in STREAMS

    def _global_id(self, name: str) -> str:
        """The globally-unique name for a local mailbox: an explicit per-mailbox
        override if set, else the server's global prefix + local name."""
        override = None
        if name in self._dynamic_mailboxes:
            override = self._dynamic_mailboxes[name].get("global_name")
        elif name in self._virtual:
            override = self._virtual[name].get("global_name")
        if override:
            return str(override)
        return f"{self._global_name}/{name}" if self._global_name else name

    # -------------------------------------------------- agent (user) registry
    def _agent_registry_path(self) -> Path:
        return Path(self.store.directory) / "agents.json"

    def _load_agent_registry(self) -> None:
        import json

        try:
            raw = json.loads(self._agent_registry_path().read_text("utf-8"))
        except Exception:
            return
        if isinstance(raw, dict):
            for name, props in raw.items():
                if isinstance(name, str):
                    self._agents[name] = props if isinstance(props, dict) else {}

    def _save_agent_registry(self) -> None:
        import json
        import os

        path = self._agent_registry_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._agents, indent=2), "utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def set_agent(self, agent_id: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create or update an agent (user) with arbitrary properties."""
        agent_id = str(agent_id or "").strip()
        if not agent_id:
            raise ValidationError("agent id is required")
        record = dict(self._agents.get(agent_id) or {})
        if isinstance(properties, dict):
            record.update(properties)
        record.setdefault("created_at", utc_now_iso())
        record["updated_at"] = utc_now_iso()
        self._agents[agent_id] = record
        self._save_agent_registry()
        return {"id": agent_id, "properties": record}

    def _virtual_message(self, mailbox: str, record: dict[str, Any]) -> dict[str, Any]:
        """Project one source record as a message for an emulated mailbox."""
        record_id = str(record.get("id") or record.get("name") or "")
        return {
            "id": f"{mailbox}:{record_id}" if record_id else f"{mailbox}:{id(record)}",
            "timestamp": record.get("updated_at") or record.get("created_at"),
            "from": "server",
            "to": record_id or mailbox,
            "send_to": mailbox,
            "text": record_id or str(record.get("text") or ""),
            "type": "RECORD",
            "mailboxId": mailbox,
            "author": "server",
            "authorName": "server",
            "mailboxName": mailbox,
            "raw": record,
        }

    @staticmethod
    def _records_from_json(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item if isinstance(item, dict) else {"value": item} for item in data]
        if isinstance(data, dict):
            records: list[dict[str, Any]] = []
            for key, value in data.items():
                records.append({"id": key, **value} if isinstance(value, dict) else {"id": key, "value": value})
            return records
        return []

    def _resolve_virtual_records(self, mailbox: str) -> list[dict[str, Any]]:
        """Resolve a virtual mailbox's source into a list of records."""
        spec = self._virtual.get(mailbox)
        if not spec:
            return []
        source = str(spec.get("source", "")).strip()
        # self endpoints, resolved internally (no HTTP self-call)
        for prefix in ("self:", "http://self/", "https://self/"):
            if source.startswith(prefix):
                endpoint = source[len(prefix):].strip("/")
                if endpoint in ("mailbox/agents", "agents"):
                    return self.mailbox_agents().get("agents", [])
                if endpoint in ("mailbox/mailboxes", "mailboxes"):
                    return self.list_mailboxes().get("mailboxes", [])
                return []
        # disk JSON file (relative paths resolve under the state directory)
        if source.startswith("disk:") or source.startswith(("file:", "./", "../", "/")) or source.endswith(".json"):
            if source.startswith("disk:"):
                path_str = source[len("disk:"):]
            elif source.startswith("file:"):
                path_str = source[len("file:"):]
            else:
                path_str = source
            path = Path(path_str)
            if not path.is_absolute():
                path = Path(self.store.directory) / path
            try:
                import json

                return self._records_from_json(json.loads(path.read_text("utf-8-sig")))
            except Exception:
                return []
        # remote http(s) endpoint â€” a participant may cap/paginate its /v1, often
        # *silently* (ask per_page=500, get its hard-capped 200, with no has_more).
        # Strategy: (1) discover the real cap from the first page's length; (2) page
        # by that EFFECTIVE size â€” for page+per_page APIs the next page is computed
        # from how many we've collected, so a 500â†’200 mismatch can't skip rows; for
        # offset APIs advance by (page-overlap); follow next_cursor when offered.
        # (3) keep going while new records appear, stopping on an empty/no-new page.
        # Dedup by id covers overlaps; _MAX_SCAN and a page ceiling bound it.
        if source.startswith(("http://", "https://")):
            import json
            import re as _re
            import urllib.request
            from urllib.parse import parse_qs, quote, urlparse

            def _fetch(url: str) -> Any:
                try:
                    with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - operator-configured
                        return json.loads(response.read().decode("utf-8"))
                except Exception:
                    return None

            def _page_records(payload: Any) -> list[dict[str, Any]]:
                if isinstance(payload, dict):
                    for key in ("messages", "events", "agents", "mailboxes", "items"):
                        if isinstance(payload.get(key), list):
                            return [it if isinstance(it, dict) else {"value": it} for it in payload[key]]
                return self._records_from_json(payload) if payload is not None else []

            def _with_param(url: str, name: str, value: Any) -> str:
                pat = rf"([?&]{_re.escape(name)}=)[^&]*"
                if _re.search(pat, url):
                    return _re.sub(pat, lambda m: m.group(1) + quote(str(value)), url)
                return f"{url}{'&' if '?' in url else '?'}{name}={quote(str(value))}"

            paging = spec.get("paging") if isinstance(spec.get("paging"), dict) else {}
            overlap = int(paging.get("overlap") or 2)
            page_step = int(paging.get("step") or 0)
            qs = parse_qs(urlparse(source).query)
            page_param = next((p for p in ("page", "pageNumber", "p") if p in qs), None)
            per_page_param = next((p for p in ("per_page", "perPage", "pageSize", "page_size") if p in qs), "per_page")
            offset_param = next((p for p in ("offset", "start", "skip", "after") if p in qs), "offset")
            mode = str(paging.get("mode") or ("page" if page_param else "offset")).lower()
            page_base = int(paging.get("base", 1))
            # Requested page size â€” a probe for the server's real cap.
            req_pp = int(paging.get("limit") or 0)
            if req_pp <= 0:
                for p in (per_page_param, "limit"):
                    if p in qs:
                        try:
                            req_pp = int(qs[p][0])
                        except Exception:
                            req_pp = 0
                        if req_pp > 0:
                            break
            if req_pp <= 0:
                req_pp = 500
            size_param = per_page_param if mode == "page" else "limit"
            source = _with_param(source, size_param, req_pp)

            records: list[dict[str, Any]] = []
            seen: set[str] = set()
            seen_cursors: set[str] = set()
            effective_pp: int | None = None
            page_num = page_base
            offset = 0
            url = _with_param(source, page_param or "page", page_num) if mode == "page" else source
            for _ in range(2000):  # page-count safety ceiling
                payload = _fetch(url)
                if payload is None:
                    break
                page = _page_records(payload)
                if not page:
                    break  # an empty page is the reliable "done" signal
                if effective_pp is None:
                    effective_pp = len(page)  # discover the server's real cap
                new = 0
                for rec in page:
                    rid = rec.get("id") if isinstance(rec, dict) else None
                    key = f"id:{rid}" if rid is not None else json.dumps(rec, sort_keys=True, default=str)
                    if key in seen:
                        continue
                    seen.add(key)
                    records.append(rec)
                    new += 1
                if len(records) >= _MAX_SCAN:
                    break
                # We NEVER trust has_more as a stop signal (peers lie or send it
                # prematurely). The only reliable ends are an empty page (handled
                # above) or a page that yields no NEW records after dedup.
                if new == 0:
                    break
                nxt = payload.get("next_cursor") if isinstance(payload, dict) else None
                if nxt and str(nxt) not in seen_cursors:
                    seen_cursors.add(str(nxt))  # next_cursor is only an advance hint
                    url = _with_param(source, "after", nxt)
                    continue
                if mode == "page":
                    pp = effective_pp or len(page) or 1
                    # Continue from the page the last collected unit lands on (by the
                    # EFFECTIVE size), realigning per_page so a silent cap can't skip.
                    page_num = (len(records) // pp) + page_base
                    url = _with_param(_with_param(source, per_page_param, pp), page_param or "page", page_num)
                else:
                    offset += page_step if page_step > 0 else max(1, len(page) - overlap)
                    url = _with_param(source, offset_param, offset)
            return records[:_MAX_SCAN]
        return []

    _MAILBOX_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

    @staticmethod
    def _activity_epoch(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def _mailbox_activity(self, name: str) -> dict[str, Any]:
        if name in self._virtual:
            return {"lastActivityAt": None, "activityPerMinute": 0, "activityPerHour": 0}
        try:
            events = self.store.tail(name, 300)
        except Exception:
            return {"lastActivityAt": None, "activityPerMinute": 0, "activityPerHour": 0}
        timestamps = [
            stamp
            for event in events
            if (stamp := self._activity_epoch(getattr(event, "ts", None))) is not None
        ]
        now = time.time()
        return {
            "lastActivityAt": max(timestamps) if timestamps else None,
            "activityPerMinute": sum(stamp >= now - 60 for stamp in timestamps),
            "activityPerHour": sum(stamp >= now - 3600 for stamp in timestamps),
        }

    def _mailbox_descriptor(self, name: str, stats: dict[str, Any] | None = None) -> dict[str, Any]:
        if stats is None:
            stats = {row["stream"]: row for row in self.store.stats()}
        v1 = "/ws_collab/v1"
        mount = "/ws_collab"
        dyn = self._dynamic_mailboxes.get(name) or {}
        return {
            "id": name,
            "name": name,
            "global_name": self._global_id(name),
            "purpose": dyn.get("purpose") or STREAM_PURPOSES.get(name, ""),
            "kind": "stream" if name in STREAMS else "mailbox",
            "source": dyn.get("source") or "jsonl",
            "origin": self._stream_origin(name),
            "transports": ["jsonl", "ws"],
            "hidden": bool(dyn.get("hidden", False)),
            "writable": self._writable_mailbox(name),
            "messages": int((stats.get(name) or {}).get("seq") or 0),
            "filename": STREAMS.get(name) or f"{name}.jsonl",
            "endpoints": {
                "read": f"{v1}/mailbox/messages?mailbox={name}",
                "send": f"{v1}/mailbox/send",
                "tail": f"{v1}/streams/{name}/tail",
                "ws": f"{mount}/ws",
            },
        }

    def list_mailboxes(self, agent: str = "", include_activity: bool = False) -> dict[str, Any]:
        """This place's directory of mailboxes: built-in streams plus any
        client-created mailboxes, each self-describing (name/purpose/source/
        transports + read/send/tail/ws endpoints). Hidden mailboxes are omitted;
        their unguessable name is the capability required to reach them."""
        stats = {row["stream"]: row for row in self.store.stats()}
        names = list(STREAMS) + sorted(self._dynamic_mailboxes)
        mailboxes = [
            self._mailbox_descriptor(name, stats)
            for name in names
            if not self._dynamic_mailboxes.get(name, {}).get("hidden")
        ]
        for vname in self._virtual:
            mailboxes.append(self._virtual_descriptor(vname))
        cursor_stats = stats if agent else {}
        for descriptor in mailboxes:
            stream_name = str(descriptor.get("id") or "")
            if include_activity:
                descriptor.update(self._mailbox_activity(stream_name))
            if agent:
                total = int((cursor_stats.get(stream_name) or {}).get("seq") or 0)
                cursor = self._mailbox_cursor_payload(stream_name, agent, total)
                descriptor.update({
                    "unread": cursor["behind"],
                    "cursorOffset": cursor["offset"],
                    "cursorInitialized": cursor["initialized"],
                    "lastReadMessageId": cursor["last_read_id"],
                    "nextUnreadMessageId": cursor["next_unread_id"],
                })
        for descriptor in mailboxes:
            self._remember_field_values(
                str(descriptor.get("id") or ""),
                [descriptor],
                observation="mailbox_definition",
            )
        self._save_field_cache()
        return {"place": "ws_collab", "global_name": self._global_name, "mailboxes": mailboxes, "server_time": utc_now_iso()}

    def mailbox_config(self, mailbox: str) -> dict[str, Any]:
        """The server's descriptor/config for a single mailbox (its real
        properties), or an empty object if the mailbox is unknown."""
        name = str(mailbox or "").strip()
        if not name:
            return {}
        if name in self._virtual:
            return self._virtual_descriptor(name)
        if name in STREAMS or name in self._dynamic_mailboxes:
            return self._mailbox_descriptor(name)
        return {}

    def _virtual_descriptor(self, name: str) -> dict[str, Any]:
        """Descriptor for an emulated read-only mailbox (projected server state)."""
        spec = self._virtual.get(name) or {}
        source = str(spec.get("source", ""))
        count = 0
        if source.rstrip("/").endswith("agents"):
            try:
                count = len(self.mailbox_agents().get("agents", []))
            except Exception:
                count = 0
        elif source.startswith("merge:"):
            try:
                count = len(self.mailbox_messages(name, limit=_MAX_SCAN).get("messages", []))
            except Exception:
                count = 0
        members = []
        if source.startswith("merge:"):
            target = source[len("merge:"):].strip()
            members = ["*"] if target in ("*", "all") else [n.strip() for n in target.split(",") if n.strip()]
        return {
            "id": name,
            "name": name,
            "global_name": self._global_id(name),
            "purpose": spec.get("purpose", ""),
            "kind": "merge" if source.startswith("merge:") else "registry",
            "source": "virtual",
            "origin": self._stream_origin(name),
            "definition": source,
            "members": members,
            "rules": spec.get("rules") or [],
            "policy": spec.get("policy") or "relay",
            "transports": ["jsonl"],
            "hidden": False,
            "writable": False,
            "messages": count,
            "filename": None,
            "endpoints": {"read": f"/ws_collab/v1/mailbox/messages?mailbox={name}"},
        }

    def create_mailbox(
        self,
        name: str,
        *,
        purpose: str = "",
        hidden: bool = False,
        writable: bool = True,
        global_name: str = "",
        source: str = "jsonl",
        rules: Any = None,
        policy: str = "",
        paging: Any = None,
        created_by: str = "operator",
    ) -> dict[str, Any]:
        """Begin hosting a new client-created mailbox (a durable JSONL stream).
        Idempotent: recreating an existing dynamic mailbox returns it unchanged.

        A virtual ``source`` (merge:/self:/disk:/http) creates a read-only projected
        stream instead; ``merge:*`` merges every real stream. ``rules``/``policy``
        add firewall-style RELAY/DROP filtering over the projected messages."""
        name = str(name or "").strip()
        if not self._MAILBOX_NAME_RE.match(name):
            raise ValidationError(
                "mailbox name must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
                details={"name": name},
            )
        if name in STREAMS:
            raise ConflictError(f"{name!r} is a built-in mailbox")
        source = str(source or "jsonl").strip()
        # A virtual source (merge:/self:/disk:/http) makes a read-only projected
        # mailbox rather than a hosted JSONL stream â€” this is what "save this
        # merge combo as a stream" produces. It is durable across restarts.
        if source.startswith(("merge:", "self:", "disk:", "http://", "https://")):
            if name in self._dynamic_mailboxes:
                raise ConflictError(f"{name!r} already exists as a hosted mailbox")
            if name in self._virtual:
                return {"created": False, "mailbox": self._virtual_descriptor(name)}
            spec: dict[str, Any] = {
                "source": source,
                "purpose": str(purpose or ""),
                "runtime": "1",
                "created_by": str(created_by or "operator"),
                "created_at": utc_now_iso(),
            }
            if global_name:
                spec["global_name"] = str(global_name)
            if isinstance(rules, list) and rules:
                spec["rules"] = [r for r in rules if isinstance(r, dict)]
            if policy:
                spec["policy"] = str(policy)
            if isinstance(paging, dict):
                pg = {k: paging[k] for k in ("limit", "step", "overlap") if isinstance(paging.get(k), (int, float))}
                if pg:
                    spec["paging"] = pg
            self._virtual[name] = spec
            self._save_virtual_registry()
            return {"created": True, "mailbox": self._virtual_descriptor(name)}
        if name in self._dynamic_mailboxes:
            return {"created": False, "mailbox": self._mailbox_descriptor(name)}
        self._dynamic_mailboxes[name] = {
            "purpose": str(purpose or ""),
            "hidden": bool(hidden),
            "writable": bool(writable),
            "global_name": str(global_name or ""),
            "source": source,
            "created_at": utc_now_iso(),
            "created_by": str(created_by or "operator"),
        }
        self.store.register_mailbox(name)
        DYNAMIC_STREAMS.add(name)
        self._save_mailbox_registry()
        return {"created": True, "mailbox": self._mailbox_descriptor(name)}

    def delete_mailbox(self, name: str) -> dict[str, Any]:
        """Stop hosting a client-created mailbox. Built-in streams cannot be
        deleted; the backing JSONL file is left on disk."""
        name = str(name or "").strip()
        if name in STREAMS:
            raise ConflictError(f"cannot delete built-in mailbox {name!r}")
        if name in self._virtual:
            if self._virtual[name].get("runtime") == "1":
                self._virtual.pop(name, None)
                self._save_virtual_registry()
                return {"id": name, "deleted": True}
            raise ConflictError(f"cannot delete config-declared virtual mailbox {name!r}")
        existed = name in self._dynamic_mailboxes
        if existed:
            self._dynamic_mailboxes.pop(name, None)
            DYNAMIC_STREAMS.discard(name)
            self._save_mailbox_registry()
        return {"id": name, "deleted": existed}

    def mailbox_agents(self) -> dict[str, Any]:
        """The users/identity directory: the operator, registered workers, agents
        in the durable registry, and any distinct source_ids seen in the
        conversation. Registry properties are merged in; ``id`` and ``kind`` win."""
        directory: dict[str, dict[str, Any]] = {}

        def ensure(agent_id: str) -> dict[str, Any]:
            agent_id = str(agent_id or "").strip()
            entry = directory.get(agent_id)
            if entry is None and agent_id:
                entry = {**(self._agents.get(agent_id) or {}), "id": agent_id}
                directory[agent_id] = entry
            return entry or {}

        ensure("operator")["kind"] = "operator"
        for worker in self.workers.list_workers():
            worker_id = str(worker.get("worker_id") or worker.get("id") or "")
            if worker_id:
                entry = ensure(worker_id)
                entry.update({key: value for key, value in worker.items() if key != "id"})
                entry["kind"] = "worker"
        for agent_id in list(self._agents):
            ensure(agent_id).setdefault("kind", "agent")
        try:
            for event in self.store.tail("conversation", 500):
                ensure(str(event.to_dict().get("source_id") or "")).setdefault("kind", "agent")
        except Exception:
            pass
        return {"agents": [entry for entry in directory.values() if entry.get("id")]}

    def _apply_filter_rules(
        self, messages: list[dict[str, Any]], rules: Any, policy: Any = None
    ) -> list[dict[str, Any]]:
        """Firewall-style, first-match RELAY/DROP filtering for a virtual stream.

        ``rules`` is an ordered list of ``{field, op, value, action}``. The first
        rule that matches a message decides its fate; if none match, the default
        ``policy`` applies (``relay`` unless set to ``drop``). Fields:
        ``any|type|text|from|to|send_to|source_id|source_kind|mailbox``. Ops:
        ``contains|equals|prefix|regex|present`` (case-insensitive). Actions and
        the policy accept synonyms: relay/accept/allow/pass vs drop/reject/deny.
        """
        import json as _json
        import re as _re

        rule_list = [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []
        if not rule_list:
            return messages
        drop_words = {"drop", "reject", "deny", "block"}
        default_relay = str(policy or "relay").strip().lower() not in drop_words

        def field_value(m: dict[str, Any], field: str) -> str:
            field = (field or "any").strip().lower()
            if field in ("any", "*", ""):
                raw = m.get("raw")
                base = " ".join(
                    str(m.get(k, "") or "")
                    for k in ("type", "text", "from", "to", "send_to", "source_id", "source_kind", "mailboxName", "mailboxId")
                )
                return base + (" " + _json.dumps(raw, default=str) if raw is not None else "")
            alias = {"mailbox": "mailboxName", "stream": "mailboxName", "sender": "from"}.get(field, field)
            return str(m.get(alias, "") or "")

        def matches(m: dict[str, Any], rule: dict[str, Any]) -> bool:
            op = str(rule.get("op", "contains")).strip().lower()
            val = str(rule.get("value", ""))
            hay = field_value(m, str(rule.get("field", "any")))
            if op == "present":
                return hay.strip() != ""
            if op == "equals":
                return hay.lower() == val.lower()
            if op == "prefix":
                return hay.lower().startswith(val.lower())
            if op == "regex":
                try:
                    return _re.search(val, hay, _re.IGNORECASE) is not None
                except _re.error:
                    return False
            return val.lower() in hay.lower()

        out: list[dict[str, Any]] = []
        for m in messages:
            relay = default_relay
            for rule in rule_list:
                if matches(m, rule):
                    relay = str(rule.get("action", "relay")).strip().lower() not in drop_words
                    break
            if relay:
                out.append(m)
        return out

    def mailbox_messages(
        self,
        mailbox: str,
        *,
        to: str | None = None,
        sender: str | None = None,
        send_to: str | None = None,
        text: str | None = None,
        do_filter: bool = False,
        limit: int = 300,
        filters: dict[str, Any] | None = None,
        _chain: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """Messages in a mailbox = events in that stream (newest last).

        Unknown mailboxes read as empty rather than erroring. When ``do_filter``
        is set, the require-match bar constraints apply: ``sender`` is the chat's
        YOU (the message ``from``), ``to`` the addressed recipient, ``send_to``
        the routed mailbox (null == this mailbox) and ``text`` a case-insensitive
        substring."""
        if mailbox in self._virtual:
            # Loop prevention: a virtual stream already being resolved higher in the
            # chain reads as empty (breaks virtualâ†’virtual cycles at the call level).
            if mailbox in _chain:
                return {"messages": [], "user": sender or "", "peer": mailbox}
            child_chain = _chain | {mailbox}
            spec = self._virtual[mailbox]
            source = str(spec.get("source", ""))
            if source.startswith("merge:"):
                target = source[len("merge:"):].strip()
                if target in ("*", "all"):
                    # "all streams" = the real streams only (built-in + dynamic),
                    # never other virtuals, so * stays a flat fan-in.
                    subs = [s for s in STREAMS]
                    subs += [m for m, meta in self._dynamic_mailboxes.items() if not (isinstance(meta, dict) and meta.get("hidden"))]
                else:
                    subs = [n.strip() for n in target.split(",") if n.strip()]
                merged: list[dict[str, Any]] = []
                for sub in subs:
                    if sub == mailbox or sub in child_chain:
                        continue  # skip self and any stream already in the relay chain
                    # Read sources unbounded â€” limit applies to the produced stream only.
                    merged.extend(self.mailbox_messages(sub, limit=_MAX_SCAN, _chain=child_chain).get("messages", []))
                merged.sort(key=lambda m: str(m.get("timestamp") or ""))
                messages = merged
            else:
                records = self._resolve_virtual_records(mailbox)
                messages = [self._virtual_message(mailbox, record) for record in records]
            messages = self._apply_filter_rules(messages, spec.get("rules"), spec.get("policy"))
            # Stamp provenance and drop anything this stream already forwarded
            # (message-level loop prevention across diamond/merge topologies).
            relayed: list[dict[str, Any]] = []
            for m in messages:
                fwd = list(m.get("forwarded_by") or [])
                if mailbox in fwd:
                    continue
                relayed.append({**m, "forwarded_by": [*fwd, mailbox]})
            messages = relayed
            if do_filter and text:
                needle = text.lower()
                messages = [m for m in messages if needle in (m.get("text") or "").lower()]
            # Single-source virtuals (self/disk/http) cache their own candidates;
            # merge members are cached by their leaf reads.
            if not source.startswith("merge:"):
                self._remember_field_values(mailbox, messages)
                self._save_field_cache()
            # Limit the PRODUCED stream only, after filtering.
            if limit and limit > 0:
                messages = messages[-min(limit, _MAX_SCAN):]
            return {"messages": messages, "user": sender or "", "peer": mailbox}
        if not self._known_mailbox(mailbox):
            return {"messages": [], "user": sender or "", "peer": mailbox}
        events = self.store.tail(mailbox, max(1, min(limit, _MAX_SCAN)), _build_predicate(filters))
        messages = [self._event_to_message(event.to_dict()) for event in events]
        # Remember field values (from the full read, before the require-match filter)
        # so the pickers have candidates for this stream.
        self._remember_field_values(mailbox, messages)
        self._save_field_cache()
        if do_filter:
            needle = (text or "").lower()

            def keep(message: dict[str, Any]) -> bool:
                if sender and message.get("from") != sender:
                    return False
                if to and message.get("to") != to:
                    return False
                if send_to and (message.get("send_to") or message.get("mailboxId")) != send_to:
                    return False
                if needle and needle not in (message.get("text") or "").lower():
                    return False
                return True

            messages = [message for message in messages if keep(message)]
        return {"messages": messages, "user": sender or "", "peer": mailbox}

    def mailbox_send(
        self,
        *,
        to: str,
        text: str,
        sender: str,
        source_kind: str = "agent",
        send_to: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Post into a mailbox. The message is published to the topic named by
        the mailbox: an explicit SEND-TO mailbox wins, else the addressed name
        when it is itself a mailbox/topic, else the shared conversation. The
        sender is recorded as the event source; a distinct recipient (an agent
        rather than a topic) is kept in ``data['to']``."""
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("message 'text' is required")
        # send_to / to are mailbox names (topics). Route to the first that names a
        # real mailbox; a non-mailbox value (e.g. an agent or the default peer)
        # simply falls through to the shared conversation.
        topic = next((name for name in (send_to, to) if name and self._writable_mailbox(name)), STREAM_CONVERSATION)
        data: dict[str, Any] = {"text": text}
        if send_to:
            data["send_to"] = send_to
        if to and to != topic:
            data["to"] = to
        published = self.publish(
            stream=topic,
            type=CONVERSATION_MESSAGE,
            data=data,
            source_id=sender,
            source_kind=source_kind,
            idempotency_key=idempotency_key,
        )
        message = self._event_to_message({
            "id": published.get("id"),
            "ts": published.get("server_time"),
            "source_id": sender,
            "source_kind": source_kind,
            "type": CONVERSATION_MESSAGE,
            "stream": topic,
            "data": data,
        })
        return {"message": {**message, "seq": published.get("seq"), "cursor": published.get("cursor")}}

    def _mailbox_cursor_payload(self, mailbox: str, agent: str, total: int) -> dict[str, Any]:
        position = self.cursors.get(mailbox, agent)
        consumed = max(0, min(total, position.seq if position else 0))
        last_read_id = None
        next_unread_id = None
        if mailbox in STREAMS or mailbox in self._dynamic_mailboxes:
            try:
                stream = self.store.stream(mailbox)
                if consumed > 0:
                    previous = stream.read(stream.cursor_at_seq(consumed - 1), 1).events
                    if previous:
                        last_read_id = previous[0].id
                if consumed < total:
                    upcoming = stream.read(stream.cursor_at_seq(consumed), 1).events
                    if upcoming:
                        next_unread_id = upcoming[0].id
            except Exception:
                pass
        return {
            "mailbox": mailbox,
            "agent": agent,
            "initialized": position is not None,
            "offset": consumed,
            "size": total,
            "behind": max(0, total - consumed),
            "entries_consumed": consumed,
            "entry_next": next_unread_id,
            "entries_total": total,
            "last_read_id": last_read_id,
            "next_unread_id": next_unread_id,
        }

    def mailbox_cursor(self, mailbox: str, agent: str) -> dict[str, Any]:
        """Durable personal cursor and messages remaining beyond it."""
        stats = {row["stream"]: row for row in self.store.stats()}
        total = int((stats.get(mailbox) or {}).get("seq") or 0)
        return self._mailbox_cursor_payload(mailbox, agent, total)

    def mailbox_cursor_move(self, mailbox: str, agent: str, start: str = "now") -> dict[str, Any]:
        if not mailbox or not agent:
            raise ValidationError("mailbox and agent are required")
        if mailbox not in STREAMS and mailbox not in self._dynamic_mailboxes:
            return self.mailbox_cursor(mailbox, agent)
        state = self.store.stream(mailbox)
        token = state.cursor_at_start() if start == "beginning" else state.cursor_at_end()
        self.cursors.reset(
            mailbox,
            agent,
            token,
            reason=f"mailbox UI moved cursor to {start}",
            operator=agent,
        )
        return self.mailbox_cursor(mailbox, agent)

    def mailbox_cursor_clear(self, mailbox: str, agent: str) -> dict[str, Any]:
        self.cursors.delete(mailbox, agent)
        return self.mailbox_cursor(mailbox, agent)

    def mailbox_record(self, record_id: str, record: dict[str, Any], mode: str = "at-end") -> dict[str, Any]:
        """Persist an edited record. Streams are append-only, so both "in-place"
        and "at-end" append a corrected event (tagged with the id it edits)
        rather than mutating the durable log."""
        if not record_id or not isinstance(record, dict):
            raise ValidationError("id and record object are required")
        raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
        inner = record.get("data") if isinstance(record.get("data"), dict) else None
        if inner is None and isinstance(raw.get("data"), dict):
            inner = raw["data"]
        if inner is None:
            inner = {"text": record.get("text") or ""}
        stream = record.get("stream") or record.get("mailboxId") or raw.get("stream") or "conversation"
        if not self._writable_mailbox(stream):
            raise ValidationError(f"mailbox {stream!r} is not writable", details={"mailbox": stream})
        event_type = record.get("type") or raw.get("type") or CONVERSATION_MESSAGE
        source_id = record.get("source_id") or record.get("author") or record.get("from") or raw.get("source_id") or "operator"
        source_kind = record.get("source_kind") or raw.get("source_kind") or "operator"
        payload = {**inner, "edited_from": record_id, "edit_mode": mode}
        published = self.publish(
            stream=stream,
            type=event_type,
            data=payload,
            source_id=source_id,
            source_kind=source_kind,
        )
        message = self._event_to_message({
            "id": published.get("id"),
            "ts": published.get("server_time"),
            "source_id": source_id,
            "source_kind": source_kind,
            "type": event_type,
            "stream": stream,
            "data": payload,
        })
        return {"entryKey": published.get("id"), "mailbox": stream, "record": message}

    # ------------------------------------------------------------------ workers
    def register_worker(self, worker_id: str, task: str = "", meta: dict[str, Any] | None = None) -> dict[str, Any]:
        if not worker_id:
            raise ValidationError("worker_id is required")
        return self.workers.register(worker_id, task, meta)

    def worker_status(self, worker_id: str, status: str, data: dict[str, Any] | None = None, errors: list[str] | None = None) -> dict[str, Any]:
        if not worker_id:
            raise ValidationError("worker_id is required")
        return self.workers.record_status(worker_id, status, data, errors=errors)

    def list_workers(self) -> dict[str, Any]:
        return {"workers": self.workers.list_workers(), "server_time": utc_now_iso()}

    def run_monitor_cycle(self) -> dict[str, Any]:
        alerts = self.workers.evaluate()
        return {"alerts": alerts, "workers": self.workers.list_workers()}

    def _announce(self, worker_id: str, message: str) -> None:
        # Route unresponsive-worker announcements through the TTS queue at high priority.
        try:
            profile = self.voices.get_profile("system") or self.voices.get_profile(worker_id)
            voice = profile.voice_id if profile else "fake:aria"
            self.tts.speak("system", message, voice_id=voice, priority=1, dedupe=True)
        except Exception:
            pass

    # -------------------------------------------------------------- speech in
    def _stt_engines_for_segment(self, segment: AudioSegment) -> list[Any]:
        if (segment.route or {}).get("audio_source") != "companion_heard_meeting_audio":
            return self.stt_engines
        return [
            engine for engine in self.stt_engines
            if not str(getattr(engine, "name", "")).lower().replace("-", "_").startswith("google_meet")
        ]

    async def process_segment(self, segment: AudioSegment) -> dict[str, Any]:
        """Run one segment through STT -> disambiguation -> classification."""

        async def on_partial(correlation_id: str, hyp: Hypothesis) -> None:
            self.publish(
                stream=STREAM_STT_TRANSCRIPTS,
                type=STT_PARTIAL_RESULT,
                data={"segment_id": segment.id, **hyp.public()},
                source_id=hyp.engine,
                source_kind="system",
                correlation_id=correlation_id,
            )

        engines = self._stt_engines_for_segment(segment)
        hypotheses = await run_stt(
            engines,
            segment,
            timeout_ms=self.config.stt_timeout_ms,
            concurrency=self.config.stt_concurrency,
            on_partial=on_partial,
        )
        for hyp in hypotheses:
            self.publish(
                stream=STREAM_STT_TRANSCRIPTS,
                type=STT_ENGINE_ERROR if hyp.error else STT_FINAL_RESULT,
                data={"segment_id": segment.id, "segment_source": (segment.route or {}).get("audio_source"), **hyp.public()},
                source_id=hyp.engine,
                source_kind="system",
                correlation_id=segment.correlation_id,
            )

        return self._finalize(segment, hypotheses)

    def _finalize(self, segment: AudioSegment, hypotheses: list[Hypothesis]) -> dict[str, Any]:
        """Resolve hypotheses, classify the source, handle echo, emit HEARD_SPEECH.

        Shared by the live capture pipeline and the external-recognizer ingest
        bridge so both produce identical resolved transcripts and timeline events.
        """

        resolved = self.disambiguator.resolve(hypotheses, context={"language": "en"})
        self.publish(
            stream=STREAM_STT_TRANSCRIPTS,
            type=TRANSCRIPT_RESOLVED,
            data={"segment_id": segment.id, "segment_source": (segment.route or {}).get("audio_source"), **resolved.public()},
            source_id="disambiguator",
            source_kind="system",
            correlation_id=segment.correlation_id,
        )

        classification = self.classifier.classify(
            segment, resolved.resolved_text, active_tts=self.tts.active_expected_texts()
        )

        # TTS echo handling + accuracy measurement.
        expected = segment.expected_tts_text or classification.expected_text
        if classification.is_echo:
            self.publish(
                stream=STREAM_TRANSLATED_AUDIO,
                type=TTS_AUDIO_DETECTED_BY_MICROPHONE,
                data={"segment_id": segment.id, "classification": classification.public()},
                source_id="capture",
                source_kind="system",
                correlation_id=segment.correlation_id,
            )
            if expected:
                report = accuracy_metrics.evaluate_pipeline(
                    expected,
                    {h.engine: h.raw_text for h in hypotheses if not h.error},
                    resolved.resolved_text,
                )
                self.accuracy.add("final", report["final"], example={"expected": expected, "got": resolved.resolved_text})
                for engine, metrics in report["per_engine"].items():
                    self.accuracy.add(engine, metrics)
                self.publish(
                    stream=STREAM_TTS,
                    type=TTS_TRANSCRIPTION_EVALUATED,
                    data={"segment_id": segment.id, "tts_event_id": classification.matched_tts_event_id, **report},
                    source_id="accuracy",
                    source_kind="system",
                    correlation_id=segment.correlation_id,
                )
            self.publish(
                stream=STREAM_TRANSLATED_AUDIO,
                type=TRANSCRIPT_FILTERED,
                data={"segment_id": segment.id, "reason": "classified as TTS/agent echo", "classification": classification.public()},
                source_id="classifier",
                source_kind="system",
                correlation_id=segment.correlation_id,
            )

        heard = self.publish(
            stream=STREAM_TRANSLATED_AUDIO,
            type=HEARD_SPEECH,
            data={
                "segment": segment.public(),
                "resolved": resolved.public(),
                "classification": classification.public(),
            },
            source_id=(segment.route or {}).get("source") or segment.source_kind,
            source_kind=segment.source_kind if segment.source_kind in {"operator", "agent", "system", "client", "worker", "companion_heard"} else "unknown",
            correlation_id=segment.correlation_id,
        )
        return {
            "correlation_id": segment.correlation_id,
            "segment_id": segment.id,
            "heard_event_id": heard["id"],
            "resolved": resolved.public(),
            "classification": classification.public(),
            "hypotheses": [h.public() for h in hypotheses],
        }

    def ingest_transcript(
        self,
        *,
        engine: str,
        text: str,
        correlation_id: str | None = None,
        confidence: float = 0.9,
        is_final: bool = True,
        language: str = "en",
        source_kind: str = "operator",
        expected_tts_text: str | None = None,
        resolve: bool = True,
        device_id: str = "external",
        audio_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ingest an already-recognised transcript from an external recognizer.

        This is the bridge for pushing speech events from another application
        (for example the Copilot app's voice dictation / Nemotron recognizer) into
        WS_COLLAB. The external engine is recorded as an STT hypothesis and, when
        final, flows through the same disambiguation, classification, and timeline
        pipeline a local engine would.
        """

        from .ids import new_event_id
        from .stt.base import normalize_text

        if not engine:
            raise ValidationError("engine is required for an external transcript")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("text is required for an external transcript")
        correlation_id = correlation_id or new_event_id()
        hypothesis = Hypothesis(
            engine=engine,
            model=f"external:{engine}",
            raw_text=text,
            normalized_text=normalize_text(text),
            confidence=max(0.0, min(1.0, float(confidence))),
            language=language,
            is_final=is_final,
        )
        self.publish(
            stream=STREAM_STT_TRANSCRIPTS,
            type=STT_FINAL_RESULT if is_final else STT_PARTIAL_RESULT,
            data={"external": True, **hypothesis.public(), **(audio_meta or {})},
            source_id=engine,
            source_kind="system",
            correlation_id=correlation_id,
        )
        if not (is_final and resolve):
            return {"correlation_id": correlation_id, "recorded": True, "resolved": None}
        segment = AudioSegment(
            correlation_id=correlation_id,
            reference_text=text,
            source_kind=source_kind,
            device_id=device_id,
            expected_tts_text=expected_tts_text,
        )
        return self._finalize(segment, [hypothesis])

    # -------------------------------------------------------------- speech out
    def speak(
        self,
        agent_id: str,
        text: str,
        *,
        priority: int | None = None,
        interrupt: bool = False,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        profile = self.voices.get_profile(agent_id)
        if profile and not profile.speaking_permission:
            raise ConflictError(f"agent {agent_id!r} does not have speaking permission")
        if profile and len(text) > profile.max_utterance_chars:
            raise ValidationError("utterance exceeds the agent's max_utterance_chars")
        resolution = self.voices.resolve_for_speak(agent_id)
        engine_voice, params = self.voices.effective_voice(resolution["voice_id"])
        effective_priority = priority if priority is not None else (profile.queue_priority if profile else 5)
        base_rate = profile.rate if profile else 1.0
        base_pitch = profile.pitch if profile else 0.0
        base_volume = profile.volume if profile else 1.0
        result = self.tts.speak(
            agent_id,
            text,
            voice_id=engine_voice,
            requested_voice_id=resolution["requested_voice_id"],
            rate=base_rate * params.get("rate", 1.0),
            pitch=base_pitch + params.get("pitch", 0.0),
            volume=base_volume * params.get("volume", 1.0),
            device=profile.output_device if profile else "default",
            priority=effective_priority,
            correlation_id=correlation_id,
            interrupt=interrupt,
        )
        result["voice_resolution"] = resolution
        return result

    async def measure_tts_accuracy(self, agent_id: str, text: str) -> dict[str, Any]:
        """Speak text, then feed it back as a loopback echo to measure accuracy."""

        spoken = self.speak(agent_id, text)
        correlation_id = spoken.get("id")
        if self.capture.listening:
            await self.capture.inject_utterance(
                text,
                source_kind="system",
                correlation_id=correlation_id,
                is_loopback=True,
                expected_tts_text=text,
                tts_event_id=spoken.get("id"),
            )
        return {"spoken": spoken, "accuracy_summary": self.accuracy.summary()}

    def accuracy_summary(self) -> dict[str, Any]:
        return {"groups": self.accuracy.summary(), "server_time": utc_now_iso()}

    # --------------------------------------------------------------- devices
    def list_devices(self) -> dict[str, Any]:
        return {"devices": self.devices.list(), "generation": self.devices.generation}

    def refresh_devices(self) -> dict[str, Any]:
        self.devices.refresh()
        return self.list_devices()

    def list_voices(self) -> dict[str, Any]:
        return {
            "voices": self.voices.list_voices(),
            "profiles": self._voice_profiles_with_activity(),
            "clones": self.voices.list_clones(),
        }

    def _voice_profiles_with_activity(self) -> list[dict[str, Any]]:
        """Enriches each raw voice profile with real, already-tracked
        activity signals from OTHER subsystems -- never fabricated:
        last_spoken_text/at (TtsEngine.last_spoken -- actual playback, not
        just enqueue), last_seen_at + worker_state (WorkerMonitor, keyed by
        agent_id == worker_id, the harness's own presence/heartbeat
        concept), and a derived `status` combining both with the engine's
        live mute/speaking state. Used by the admin UI's Virtual agents
        tables (Agent Voices page, and the Google Meet page's summary)."""
        tts_state = self.tts.state()
        muted_agents = set(tts_state.get("muted_agents") or [])
        speaking_agent = ((tts_state.get("current") or {}) or {}).get("agent_id")
        profiles = self.voices.list_profiles()
        for profile in profiles:
            agent_id = profile.get("agent_id")
            spoken = self.tts.last_spoken(agent_id) if agent_id else None
            profile["last_spoken_text"] = spoken["text"] if spoken else None
            profile["last_spoken_at"] = spoken["at"] if spoken else None
            worker = self.workers.snapshot(agent_id) if agent_id else {}
            profile["last_seen_at"] = worker.get("last_status_at")
            profile["worker_state"] = worker.get("state")
            if not profile.get("voice_id"):
                status = "unassigned"
            elif agent_id in muted_agents:
                status = "muted"
            elif agent_id and agent_id == speaking_agent:
                status = "speaking"
            elif worker.get("state") and worker["state"] != "ok":
                status = worker["state"]
            else:
                status = "idle"
            profile["status"] = status
        return profiles

    def clone_voice(self, base_voice_id: str, name: str, *, rate: float = 1.0, pitch: float = 0.0,
                    volume: float = 1.0, style: str = "", operator: str = "operator") -> dict[str, Any]:
        return self.voices.clone_voice(
            base_voice_id, name, rate=rate, pitch=pitch, volume=volume, style=style, operator=operator
        )

    def delete_clone(self, clone_id: str, operator: str = "operator") -> dict[str, Any]:
        return {"deleted": self.voices.delete_clone(clone_id, operator=operator), "clone_id": clone_id}

    def convert_representation(self, value: Any = None, to: str = "metta",
                              items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Render a value (or a batch of values) as MeTTa or pretty JSON.

        Server-side port of the workbench's markdown/MeTTa codec so clients can
        display JSON/MeTTa views without duplicating the codec.
        """

        from . import metta_codec

        if items is not None:
            return {
                "to": to,
                "results": [
                    {"id": item.get("id"), "text": metta_codec.convert_value(item.get("value"), to)}
                    for item in items
                ],
            }
        return {"to": to, "text": metta_codec.convert_value(value, to)}

    def set_voice_profile(self, agent_id: str, updates: dict[str, Any], operator: str = "operator") -> dict[str, Any]:
        return self.voices.set_profile(agent_id, updates, operator=operator).public()

    def assign_voices(self, policy: str | None = None) -> dict[str, Any]:
        agents = [{"agent_id": a} for a in self.config.agents] or [{"agent_id": p["agent_id"]} for p in self.voices.list_profiles()]
        return self.voices.auto_assign(agents, policy)

    # --------------------------------------------------------------- routing
    def routing_matrix(self) -> dict[str, Any]:
        return {"routes": self.routing.matrix()}

    def stt_engine_routes(self) -> dict[str, Any]:
        """One row per configured STT engine with the device it listens on.

        Engines without an explicit route fall back to the active capture device,
        which is reported so the UI can show what is actually in use rather than
        an empty cell.
        """

        capture = self.capture.state()
        rows = []
        for engine in self.stt_engines:
            route = self.routing.get(DEFAULT_ROUTE_SOURCE, engine.name)
            device_id = route.device_id if route else ""
            device = self.devices.get(device_id) if device_id else None
            rows.append({
                "engine": engine.name,
                "model": getattr(engine, "model", ""),
                "is_remote": getattr(engine, "is_remote", False),
                "device_id": device_id,
                "device_name": device.name if device else None,
                "explicit": route is not None,
                "effective_device_id": device_id or capture.get("device_id"),
                "effective_device_name": (device.name if device else capture.get("device_name")),
            })
        return {"engines": rows, "source": DEFAULT_ROUTE_SOURCE}

    def set_engine_device(self, engine: str, device_id: str, *, operator: str = "operator") -> dict[str, Any]:
        """Point one STT engine at a specific input device."""

        known = {e.name for e in self.stt_engines}
        if engine not in known:
            raise ValidationError(f"unknown STT engine: {engine!r}", details={"engines": sorted(known)})
        if not device_id:
            self.routing.delete_route(DEFAULT_ROUTE_SOURCE, engine, operator=operator)
            self.sound_settings.set_engine_device(engine, None)
            return {"engine": engine, "device_id": None, "cleared": True}
        device = self.devices.get(device_id)
        if device is None:
            raise NotFoundError(f"unknown device: {device_id}")
        if device.direction not in ("input", "loopback", "virtual"):
            raise ValidationError(
                "an STT engine must listen on a capture-capable device",
                details={"device": device.name, "direction": device.direction},
            )
        route = self.routing.set_route(
            DEFAULT_ROUTE_SOURCE, engine, device_id, operator=operator,
            # Loopback captures the machine's own output, so it is diagnostic and
            # TTS-accuracy material -- never a source of operator commands.
            command_eligible=device.direction != "loopback",
            tts_accuracy_eligible=device.direction == "loopback",
        )
        self.sound_settings.set_engine_device(engine, device_id)
        return route.public()

    # ------------------------------------------------------- audio defaults
    @property
    def _audio_defaults_path(self) -> Path:
        return Path(self.config.state_dir) / "audio_defaults.json"

    def _legacy_agent_output_device(self) -> str:
        """Read the agent output device from the pre-consolidation legacy file."""

        import json

        path = self._audio_defaults_path
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data.get("agent_output_device") or ""
            except (OSError, ValueError):
                return ""
        return ""

    def get_audio_defaults(self) -> dict[str, Any]:
        device_id = self.sound_settings.get("agent_output_device") or ""
        if not device_id:
            # One-time migration from the legacy audio_defaults.json file.
            migrated = self._legacy_agent_output_device()
            if migrated:
                device_id = migrated
                self.sound_settings.set("agent_output_device", migrated)
        device = self.devices.get(device_id) if device_id else None
        if device_id and device is None:
            # Persisted device is gone (unplugged, host API changed).
            fallback = self.devices.default_output()
            return {
                "agent_output_device": device_id,
                "agent_output_device_name": None,
                "available": False,
                "note": "the saved output device is not currently present",
                "effective_device_id": fallback.id if fallback else None,
                "effective_device_name": fallback.name if fallback else None,
            }
        fallback = self.devices.default_output()
        return {
            "agent_output_device": device_id or None,
            "agent_output_device_name": device.name if device else None,
            "available": True,
            "effective_device_id": device_id or (fallback.id if fallback else None),
            "effective_device_name": (device.name if device else (fallback.name if fallback else None)),
        }

    def set_default_output_device(self, device_id: str, *, operator: str = "operator") -> dict[str, Any]:
        """Set the output device agents speak through by default."""

        if device_id:
            device = self.devices.get(device_id)
            if device is None:
                raise NotFoundError(f"unknown device: {device_id}")
            if device.direction not in ("output", "virtual"):
                raise ValidationError(
                    "agents must speak through a playback-capable device",
                    details={"device": device.name, "direction": device.direction},
                )
        self.sound_settings.set("agent_output_device", device_id or None)

        # Agents that never chose their own output device resolve the default at
        # speak time, so this change takes effect immediately with no per-profile
        # writes to make here.
        self._audit_sink({
            "type": "AUDIO_DEFAULT_CHANGED", "action": "set_agent_output_device",
            "device_id": device_id or None, "operator": operator,
        })
        return self.get_audio_defaults()

    def preview_voice(self, voice_id: str, *, text: str | None = None, rate: float = 1.0,
                      pitch: float = 0.0, volume: float = 1.0) -> dict[str, Any]:
        """Speak a short sample with a specific voice (and optional rate/pitch).

        Independent of any agent profile, so the admin UI can audition any voice
        -- including cloned presets -- before assigning it.
        """

        voice = self.voices.get_voice(voice_id)
        if voice is None:
            raise NotFoundError(f"unknown voice: {voice_id}")
        engine_voice, params = self.voices.effective_voice(voice_id)
        sample = text or f"This is {voice.name}, a {voice.language} voice."
        return self.tts.speak(
            "preview",
            sample,
            voice_id=engine_voice,
            requested_voice_id=voice_id,
            rate=float(rate) * params.get("rate", 1.0),
            pitch=float(pitch) + params.get("pitch", 0.0),
            volume=float(volume) * params.get("volume", 1.0),
            priority=1,
            dedupe=False,
        )

    def _play_test_tone(self, device: Any) -> bool:
        """Best-effort: play a short sine tone on a real output device."""

        if getattr(device, "backend", "") != "sounddevice" or device.backend_index is None:
            return False
        try:  # pragma: no cover - depends on real audio hardware
            import numpy as np
            import sounddevice as sd

            sample_rate = 44100
            duration = 0.6
            t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
            tone = (0.2 * np.sin(2 * np.pi * 440.0 * t)).astype("float32")
            sd.play(tone, samplerate=sample_rate, device=device.backend_index)
            return True
        except Exception:
            return False

    def test_output_device(self, device_id: str, *, text: str | None = None) -> dict[str, Any]:
        """Play a test sound on a specific output device.

        Prefers a real tone on the exact device; falls back to a spoken test
        phrase through the TTS engine when a direct tone is not possible.
        """

        device = self.devices.get(device_id)
        if device is None:
            raise NotFoundError(f"unknown device: {device_id}")
        if device.direction not in ("output", "virtual"):
            raise ValidationError(
                "a test sound needs a playback-capable device",
                details={"device": device.name, "direction": device.direction},
            )
        if self._play_test_tone(device):
            self._audit_sink({"type": "AUDIO_TEST", "action": "test_output_device",
                              "device_id": device_id, "method": "tone"})
            return {"device_id": device_id, "device_name": device.name, "method": "tone"}
        voices = self.voices.list_voices()
        voice_id = voices[0]["id"] if voices else "fake:aria"
        result = self.tts.speak(
            "device-test", text or f"Test sound for {device.name}.",
            voice_id=voice_id, device=device_id, priority=1, dedupe=False,
        )
        return {"device_id": device_id, "device_name": device.name, "method": "tts", "tts": result}

    def set_route(self, source: str, engine: str, device_id: str, operator: str = "operator", **params: Any) -> dict[str, Any]:
        return self.routing.set_route(source, engine, device_id, operator=operator, **params).public()

    # ---------------------------------------------------------------- capture
    def start_capture(self, device_id: str | None = None) -> dict[str, Any]:
        state = self.capture.start(device_id=device_id)
        # Persist the active input device so it survives a restart.
        self.sound_settings.set("capture_device", state.get("device_id") or None)
        return state

    def stop_capture(self) -> dict[str, Any]:
        return self.capture.stop()

    def capture_state(self) -> dict[str, Any]:
        return self.capture.state()

    def start_secondary_capture(self, device_id: str) -> dict[str, Any]:
        return self.secondary_capture.start(device_id=device_id)

    def stop_secondary_capture(self) -> dict[str, Any]:
        return self.secondary_capture.stop()

    def secondary_capture_state(self) -> dict[str, Any]:
        return self.secondary_capture.state()

    def set_echo_policy(self, policy: str) -> dict[str, Any]:
        """Change how captured audio is reconciled against our own TTS, live.

        Both the capture service (mute-during-tts check) and the classifier
        (echo detection) read ``echo_policy`` on every use, not just at
        startup, so updating both here takes effect immediately -- no
        restart needed.
        """

        if policy not in ECHO_POLICIES:
            raise ValidationError(f"invalid echo policy: {policy!r}", details={"allowed": sorted(ECHO_POLICIES)})
        previous = self.config.echo_policy
        self.config.echo_policy = policy
        self.classifier.echo_policy = policy
        self.sound_settings.set("echo_policy", policy)
        self._audit_sink({"type": "AUDIO_POLICY_CHANGED", "action": "set_echo_policy", "previous": previous, "policy": policy})
        return self.capture.state()

    # ----------------------------------------------------------------- cursors
    def cursor_get(self, stream: str, consumer: str) -> dict[str, Any]:
        if stream not in STREAMS:
            raise ValidationError(f"unknown stream: {stream!r}")
        position = self.cursors.get(stream, consumer)
        if position is None:
            return {"stream": stream, "consumer": consumer, "token": self.store.stream(stream).cursor_at_start(), "seq": 0}
        return position.public()

    def cursor_list(self) -> dict[str, Any]:
        return {"cursors": self.cursors.list()}

    def cursor_commit(self, stream: str, consumer: str, token: str, reason: str = "processed") -> dict[str, Any]:
        return self.cursors.commit(stream, consumer, token, reason=reason).public()

    def cursor_reposition(
        self,
        stream: str,
        consumer: str,
        *,
        token: str | None = None,
        seq: int | None = None,
        reason: str,
        operator: str,
        allow_replay: bool = False,
        allow_skip: bool = False,
    ) -> dict[str, Any]:
        if token is None:
            if seq is None:
                raise ValidationError("either token or seq is required")
            token = self.store.stream(stream).cursor_at_seq(int(seq))
        return self.cursors.reposition(
            stream, consumer, token, reason=reason, operator=operator, allow_replay=allow_replay, allow_skip=allow_skip
        ).public()

    def cursor_reset(self, stream: str, consumer: str, *, to: str = "start", reason: str = "", operator: str = "operator") -> dict[str, Any]:
        state = self.store.stream(stream)
        token = state.cursor_at_end() if to == "end" else state.cursor_at_start()
        return self.cursors.reset(stream, consumer, token, reason=reason or f"reset to {to}", operator=operator).public()

    def cursor_history(self, stream: str, consumer: str) -> dict[str, Any]:
        return {"history": self.cursors.history(stream, consumer)}

    # ------------------------------------------------------------------ prompt
    def _prompt_history_events(self) -> list[dict[str, Any]]:
        events = self.store.tail(STREAM_PROMPT_STREAM, 500)
        return [event.to_dict() for event in events]

    def prompt_get(self) -> dict[str, Any]:
        return self.prompt.current()

    def prompt_save(self, text: str, operator: str = "operator", note: str = "") -> dict[str, Any]:
        return self.prompt.save(text, operator=operator, note=note)

    def prompt_history(self) -> dict[str, Any]:
        return {"history": self.prompt.history()}

    def prompt_preview_diff(self, text: str) -> dict[str, Any]:
        return {"diff": self.prompt.preview_diff(text)}

    def prompt_rollback(self, version: int, operator: str = "operator") -> dict[str, Any]:
        return self.prompt.rollback(version, operator=operator)

    # -------------------------------------------------------------------- misc
    def get_config_public(self) -> dict[str, Any]:
        cfg = self.config
        return {
            "host": cfg.host,
            "http_port": cfg.http_port,
            "https_port": cfg.https_port,
            "https_enabled": cfg.https_enabled,
            "state_dir": str(cfg.state_dir),
            "jsonl_dir": str(cfg.jsonl_dir),
            "admin_remote": cfg.admin_remote,
            "require_tls": cfg.require_tls,
            "rate_limit_rps": cfg.rate_limit_rps,
            "worker_thresholds": {
                "warn": cfg.worker_warn_seconds,
                "overdue": cfg.worker_overdue_seconds,
                "unresponsive": cfg.worker_unresponsive_seconds,
            },
            "audio_enabled": cfg.audio_enabled,
            "echo_policy": cfg.echo_policy,
            "stt_engines": cfg.stt_engines,
            "tts_policy": cfg.tts_policy,
            "agents": cfg.agents,
            "warnings": self._warnings,
        }

    def get_admin_ui_state(self, page: str) -> dict[str, Any]:
        return self.admin_ui_state.get_page(page)

    def set_admin_ui_state(self, page: str, state: Any) -> dict[str, Any]:
        return self.admin_ui_state.set_page(page, state)

    # ------------------------------------------------------- docs / ui / files
    # Files whose contents are never served, regardless of role. The writable
    # state directory holds the generated administrator token and session data;
    # listing their existence is fine, exposing their bytes is not.
    SECRET_FILENAMES = {"generated_admin_token.txt", ".ws_collab.lock"}
    SECRET_DIRS = {"sessions"}

    @property
    def docs_dir(self) -> Path:
        # This module lives at <plugin_root>/src/ws_collab/service.py (src
        # layout, three levels below the plugin root where docs/ actually
        # sits): parents[0] is src/ws_collab, parents[1] is src, parents[2]
        # is the plugin root. A fixed parents[1] undercounts that (a leftover
        # from before the src/ layout move) and silently finds nothing.
        return Path(__file__).resolve().parents[2] / "docs"

    def list_docs(self) -> dict[str, Any]:
        """Markdown documentation shipped with the server."""

        directory = self.docs_dir
        documents = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.md")):
                documents.append({
                    "id": path.stem.lower(),
                    "name": path.name,
                    "title": _markdown_title(path),
                    "bytes": path.stat().st_size,
                    "path": f"/ws_collab/docs/{path.name}",
                })
        readme = directory.parent / "README.md"
        if readme.is_file():
            documents.insert(0, {
                "id": "readme", "name": "README.md", "title": _markdown_title(readme),
                "bytes": readme.stat().st_size, "path": "/ws_collab/docs/README.md",
            })
        return {"documents": documents, "count": len(documents)}

    def read_doc(self, name: str) -> str:
        from .security import safe_join

        if not name.endswith(".md"):
            raise ValidationError("only markdown documents are served")
        root = self.docs_dir
        candidate = root / name
        if name == "README.md":
            candidate = root.parent / "README.md"
            if not candidate.is_file():
                raise NotFoundError(name)
            return candidate.read_text(encoding="utf-8")
        path = safe_join(root, name)
        if not path.is_file():
            raise NotFoundError(name)
        return path.read_text(encoding="utf-8")

    def ui_links(self, *, origin: str = "") -> dict[str, Any]:
        """Deep links to every page of the operations workbench."""

        origin = origin.rstrip("/")
        base = f"{origin}/ws_collab/admin/" if origin else "/ws_collab/admin/"
        pages = [
            ("transcript", "Unified Transcript", "Full speech pipeline in chronological order"),
            ("conversation", "Conversation", "Worker, agent, and human messages"),
            ("streams", "JSONL Streams", "Any durable stream, rendered or raw"),
            ("workers", "Workers", "Registry, health, and check-ins"),
            ("alerts", "Alerts", "Raised and recovered alerts"),
            ("devices", "Devices & Routing", "Audio devices, capture, routing matrix"),
            ("voices", "Agent Voices", "Voice catalog, profiles, and policies"),
            ("accuracy", "TTS Accuracy", "Rolling WER/CER by engine"),
            ("cursors", "Cursors", "Inspect and reposition durable cursors"),
            ("prompt", "Prompt", "Edit, diff, and roll back the worker prompt"),
            ("system", "System & Audit", "Health, configuration, and audit history"),
        ]
        return {
            "base": base,
            "links": [
                {"id": page, "title": title, "description": description, "url": f"{base}#{page}"}
                for page, title, description in pages
            ],
        }

    def list_files(self, subpath: str = "") -> dict[str, Any]:
        """List the writable state directory (read-only, traversal-safe)."""

        from .security import safe_join

        root = Path(self.config.state_dir).resolve()
        directory = safe_join(root, subpath) if subpath else root
        if not directory.is_dir():
            raise NotFoundError(f"not a directory: {subpath or '.'}")

        entries = []
        for path in sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name)):
            relative = path.relative_to(root).as_posix()
            secret = path.name in self.SECRET_FILENAMES or path.name in self.SECRET_DIRS
            stat = path.stat()
            entries.append({
                "name": path.name,
                "path": relative,
                "kind": "dir" if path.is_dir() else "file",
                "bytes": None if path.is_dir() else stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    .isoformat(timespec="seconds").replace("+00:00", "Z"),
                # Secrets are listed so operators know they exist, but never read.
                "readable": not secret and path.is_file(),
                "protected": secret,
            })
        return {"root": str(root), "path": subpath, "entries": entries}

    def read_file(self, subpath: str, *, max_bytes: int = 256 * 1024) -> dict[str, Any]:
        """Read a bounded tail of a file inside the writable state directory."""

        from .security import safe_join

        root = Path(self.config.state_dir).resolve()
        path = safe_join(root, subpath)
        if path.name in self.SECRET_FILENAMES or any(
            part in self.SECRET_DIRS for part in path.relative_to(root).parts[:-1]
        ):
            raise AuthorizationError(
                "this file holds credentials and is never served",
                details={"path": subpath},
            )
        if not path.is_file():
            raise NotFoundError(subpath)
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            raw = handle.read(max_bytes)
        return {
            "path": subpath,
            "bytes": size,
            "truncated": size > max_bytes,
            "content": raw.decode("utf-8", errors="replace"),
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "server_time": utc_now_iso(),
            "boot_id": self.boot_id,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "streams": self.store.stats(),
            "broker": self.broker.stats(),
            "workers": len(self.workers.list_workers()),
            "capture": self.capture.state(),
            "tts": self.tts.state(),
            "devices": self.devices.generation,
            "warnings": self._warnings,
        }

    # ------------------------------------------------------- status/readiness
    def status(self) -> dict[str, Any]:
        """Subsystem rollup with an overall verdict.

        ``/health`` answers "is the process alive"; this answers "is each part
        actually working". Deliberately coarse and free of secrets so it is safe
        to expose to a load balancer or status page.
        """

        subsystems: dict[str, dict[str, Any]] = {}

        # Durable store: the one hard dependency. If it cannot be read, we are down.
        try:
            stats = self.store.stats()
            subsystems["event_store"] = {
                "state": "ok",
                "streams": len(stats),
                "events": sum(entry.get("seq", 0) for entry in stats),
            }
        except Exception as error:  # noqa: BLE001
            subsystems["event_store"] = {"state": "down", "detail": str(error)}

        # Speech input: degraded when enabled but not actually capturing.
        capture = self.capture.state()
        if not self.config.audio_enabled:
            capture_state = "disabled"
        elif not capture["listening"]:
            capture_state = "idle"
        elif capture.get("error"):
            capture_state = "degraded"
        else:
            capture_state = "ok"
        subsystems["capture"] = {
            "state": capture_state,
            "backend": capture["backend"],
            "live": capture.get("live_capture", False),
            "device": capture.get("device_name"),
            "detail": capture.get("error"),
        }

        # Recognition: degraded when any engine fell back to a double.
        degraded_engines = [w for w in self._warnings if "STT engine" in w]
        subsystems["stt"] = {
            "state": "degraded" if degraded_engines else "ok",
            "engines": [engine.name for engine in self.stt_engines],
            "detail": degraded_engines or None,
        }

        subsystems["tts"] = {
            "state": "ok",
            "backend": self.tts.state()["backend"],
            "voices": len(self.voices.list_voices()),
            "queued": len(self.tts.state()["queue"]),
        }

        workers = self.workers.list_workers()
        unhealthy = [w for w in workers if w["state"] in ("overdue", "unresponsive")]
        subsystems["workers"] = {
            "state": "degraded" if unhealthy and len(unhealthy) == len(workers) else "ok",
            "total": len(workers),
            "unhealthy": len(unhealthy),
        }

        subsystems["transports"] = {
            "state": "ok",
            "websocket_subscribers": self.broker.stats()["subscriptions"],
            "tls": self.config.https_enabled,
        }

        # Overall verdict: anything down wins, then any degradation.
        states = [entry["state"] for entry in subsystems.values()]
        if "down" in states:
            overall = "down"
        elif "degraded" in states:
            overall = "degraded"
        else:
            overall = "ok"

        return {
            "status": overall,
            "version": __version__,
            "boot_id": self.boot_id,
            "server_time": utc_now_iso(),
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "subsystems": subsystems,
            "warnings": self._warnings,
        }

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        """Return ``(ready, body)``. Not ready => the caller should answer 503."""

        report = self.status()
        ready = report["status"] != "down"
        return ready, {
            "ready": ready,
            "status": report["status"],
            "server_time": report["server_time"],
        }

    def endpoints(self, *, origin: str = "") -> dict[str, Any]:
        """Machine-readable map of every mounted endpoint.

        Clients (including the admin page's connection inspector) resolve URLs
        from here instead of hard-coding them, so a changed prefix or scheme
        cannot leave a client pointing at the wrong place.
        """

        base = "/ws_collab"
        origin = origin.rstrip("/")
        ws_origin = ""
        if origin:
            ws_origin = ("wss://" if origin.startswith("https://") else "ws://") + origin.split("://", 1)[1]

        def http_url(path: str) -> str:
            return f"{origin}{path}" if origin else path

        def ws_url(path: str) -> str:
            return f"{ws_origin}{path}" if ws_origin else path

        # Every route answers under each of these roots.
        mounts = ["", "/v1", "/ws_collab", "/ws_collab/v1"]

        def entry(id_: str, path: str, auth: str, description: str, kind: str = "http") -> dict[str, Any]:
            full = f"{base}{path}"
            return {
                "id": id_,
                "kind": kind,
                "path": full,
                "url": ws_url(full) if kind == "ws" else http_url(full),
                "auth": auth,
                "description": description,
                "aliases": [
                    (ws_url(f"{m}{path}") if kind == "ws" else http_url(f"{m}{path}"))
                    for m in mounts
                ],
            }

        return {
            "origin": origin or None,
            "base": base,
            "mounts": mounts,
            "tls": self.config.https_enabled,
            "endpoints": [
                entry("health", "/health", "public", "Liveness: is the process running"),
                entry("status", "/status", "public", "Subsystem rollup with an overall verdict"),
                entry("ready", "/ready", "public", "Readiness probe; 503 when not serving"),
                entry("capabilities", "/capabilities", "public", "Streams, roles, and feature flags"),
                entry("endpoints", "/endpoints", "token", "This endpoint map"),
                entry("events", "/events", "token", "Cursor-paginated durable events"),
                entry("websocket", "/ws", "token", "WebSocket transport (full REST parity)", kind="ws"),
                entry("docs", "/docs", "token", "Markdown documentation shipped with the server"),
                entry("ui_links", "/ui/links", "token", "Deep links to every workbench page"),
                entry("files", "/files", "token", "Writable state directory (read-only; secrets withheld)"),
                entry("workers", "/workers", "token", "Worker registry and health"),
                entry("cursors", "/cursors", "token", "Durable consumer cursors"),
                entry("voices", "/voices", "token", "Voice catalog and agent profiles"),
                entry("devices", "/audio/devices", "token", "Enumerated audio devices"),
                entry("diagnostics", "/diagnostics", "token", "Detailed runtime diagnostics"),
                entry("audit", "/audit", "token", "Security audit history"),
                entry("admin", "/admin/", "token", "Operations workbench"),
                {"id": "openapi", "kind": "http", "path": "/openapi/docs",
                 "url": http_url("/openapi/docs"), "auth": "public",
                 "description": "Interactive OpenAPI documentation", "aliases": []},
            ],
        }

    def read_audit(self, after: str | None = None, limit: int = 100) -> dict[str, Any]:
        return self.read_events(STREAM_AUDIT, after=after, limit=limit)

    # --------------------------------------------------------------- lifecycle
    async def startup(self) -> None:
        # Let capture threads dispatch finished utterances onto the server loop.
        self.capture.bind_loop(asyncio.get_running_loop())
        await self.tts.start()
        self._seed_prompt()
        self._seed_voices()
        self._seed_workers()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def shutdown(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        await self.tts.stop()

    async def _monitor_loop(self) -> None:
        # An internal bounded server loop (allowed) -- NOT a worker keep-alive.
        interval = max(5, self.config.worker_warn_seconds // 2)
        while True:
            await asyncio.sleep(interval)
            try:
                self.workers.evaluate()
            except Exception:
                pass

    def _seed_prompt(self) -> None:
        if not self.prompt.current()["exists"]:
            self.prompt.save(DEFAULT_WORKER_PROMPT, operator="system", note="initial default prompt")

    def _seed_voices(self) -> None:
        if self.config.agents and not self.voices.list_profiles():
            try:
                self.assign_voices()
            except Exception:
                pass

    def _seed_workers(self) -> None:
        """Rehydrate the worker registry from durable status events after a restart,
        so previously-registered workers still show up (and stay assignable) instead
        of vanishing until they happen to check in again."""

        try:
            events = self.store.tail(STREAM_STATUSES, 1000)
            self.workers.rebuild_from_events([event.to_dict() for event in events])
        except Exception:
            pass


# The prompt history stream constant (imported lazily to avoid a cycle above).
from .events import STREAM_PROMPT as STREAM_PROMPT_STREAM  # noqa: E402


DEFAULT_WORKER_PROMPT = """\
WS_COLLAB worker monitoring addendum
====================================

Your existing assigned tasks remain your PRIMARY responsibility. WS_COLLAB
monitoring is an ADDITIONAL, lightweight duty layered on top of that work.

Recurring activation:
- Native Codex/Copilot automation is the ONLY approved recurring worker launcher.
- Do NOT use OS schedulers, external launchers, watchdogs, self-revival scripts,
  or scripted keep-alive/polling loops to keep yourself running.
- Each activation performs exactly ONE bounded monitoring cycle, then returns to
  your primary work.

Each monitoring cycle:
1. Read new audio, conversation, and worker-status events from your durable
   cursors (bounded page sizes only; never re-read whole files).
2. Advance each cursor only after you have successfully processed its events.
   Bounded same-activation retries and rereads are allowed; rewind only for
   deliberate replay/verification.
3. Check overall team health. If workers appear overdue/unresponsive, publish a
   status/alert; remember that you may be the last responsive observer.
4. Return to your primary task.

Honesty:
- If a requested timing/behaviour is not supported by native automation, report
  that limitation plainly instead of simulating it with a polling loop.
"""
