"""Persistent Meet browser settings for the next bridge launch."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


_MEET_ROOM_RE = re.compile(
    r"^(?:https?://meet\.google\.com/|google-meet:)?"
    r"([a-z]{3,4}-[a-z]{3,5}-[a-z]{3,4})(?:[/?#].*)?$",
    re.IGNORECASE,
)
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def _process_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


def normalize_meeting_url(value: Any) -> str:
    """Return the stable channel URL accepted by persisted meeting settings."""
    match = _MEET_ROOM_RE.fullmatch(str(value or "").strip())
    if not match:
        raise ValueError("expected a Google Meet room URL or room id")
    return f"https://meet.google.com/{match.group(1).lower()}"


def select_startup_meeting(
    explicit_meet: str | None,
    create_new: bool,
    policies: dict[str, dict[str, Any]],
    forgotten: set[str] | None = None,
) -> str | None:
    """Apply explicit launch precedence, then the deterministic autostart policy."""

    if explicit_meet or create_new:
        return explicit_meet
    tombstones = forgotten or set()
    eligible = sorted(
        key
        for key, policy in policies.items()
        if key not in tombstones
        and isinstance(policy, dict)
        and policy.get("autostart") is True
    )
    return eligible[0] if eligible else None


COMPANION_CLICK_BUILTINS: dict[str, Any] = {
    "enabled": False,
    "action": "say:uh",
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
    "f0Hz": 125.0,
    "f1Hz": 600.0,
    "f2Hz": 1300.0,
}


def companion_click_layers(
    state: dict[str, Any],
    channel_key: str = "",
    *,
    now: float | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Return ordered runtime patches: global, channel, then a live test lease."""
    layers: list[tuple[str, dict[str, Any]]] = []
    global_patch = state.get("companion_click", {})
    if isinstance(global_patch, dict):
        layers.append(("default", global_patch))
    channels = state.get("meeting_companion_click", {})
    channel_patch = channels.get(channel_key) if channel_key and isinstance(channels, dict) else None
    if isinstance(channel_patch, dict):
        layers.append(("override", channel_patch))
    active_test = state.get("active_test_companion_click", {})
    current_time = time.time() if now is None else float(now)
    if (
        channel_key
        and isinstance(active_test, dict)
        and active_test.get("channelKey") == channel_key
        and float(active_test.get("expiresAt") or 0.0) > current_time
    ):
        profile_name = str(active_test.get("testProfile") or "")
        profiles = state.get("test_companion_click", {})
        test_patch = profiles.get(profile_name) if isinstance(profiles, dict) else None
        if isinstance(test_patch, dict):
            layers.append((f"test:{profile_name}", test_patch))
    return layers


def companion_click_runtime_layers(
    state: dict[str, Any],
    channel_key: str,
    cli_seed: dict[str, Any],
    *,
    persisted_source_seen: bool = False,
    now: float | None = None,
) -> tuple[list[tuple[str, dict[str, Any]]], bool]:
    """Return refresh layers without letting deleted patches reveal launch values."""

    has_persisted_source = any(
        key in state
        for key in (
            "companion_click",
            "meeting_companion_click",
            "test_companion_click",
        )
    )
    persisted_source_seen = persisted_source_seen or has_persisted_source
    base = COMPANION_CLICK_BUILTINS if persisted_source_seen else cli_seed
    return [("built-in" if persisted_source_seen else "cli", dict(base)), *companion_click_layers(
        state, channel_key, now=now
    )], persisted_source_seen


class MeetBrowserSettings:
    """A small atomic JSON store for Meet browser launch preferences."""

    REQUIRE_SSO_CONSENT_KEY = "require_sso_consent"
    _PROFILES_KEY = "profiles"
    _LEGACY_PROFILES_KEY = "shared_profiles"
    _PROFILE_REGISTRY_KEY = "profile_registry"
    _MEETING_ROUTING_KEY = "meeting_routing"

    def __init__(self, directory: Path | str):
        self.directory = Path(directory)
        self.path = self.directory / "meet_browser_settings.json"
        self.lock_path = self.directory / ".meet_browser_settings.lock"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                self._data = {}
        else:
            self._data = {}

    @contextmanager
    def _interprocess_lock(self, timeout: float = 5.0):
        self.directory.mkdir(parents=True, exist_ok=True)
        process_lock = _process_lock(self.lock_path)
        acquired = process_lock.acquire(timeout=timeout)
        if not acquired:
            raise TimeoutError(
                f"timed out waiting for Meet browser settings lock {self.lock_path}"
            )
        handle = None
        deadline = time.monotonic() + timeout
        try:
            handle = open(self.lock_path, "a+b")
            if self.lock_path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:  # pragma: no cover - exercised on POSIX
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "timed out waiting for another process to finish updating "
                            f"Meet browser settings at {self.path}"
                        ) from error
                    time.sleep(0.02)
            yield
        finally:
            if handle is not None:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                handle.close()
            process_lock.release()

    @contextmanager
    def _mutation(self):
        with self._lock:
            with self._interprocess_lock():
                self._load_unlocked()
                yield

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.directory / (
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            tmp.write_text(
                json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8"
            )
            for attempt in range(6):
                try:
                    os.replace(tmp, self.path)
                    return
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.02 * (2**attempt))
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def all(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._mutation():
            if value in (None, ""):
                self._data.pop(key, None)
            else:
                self._data[key] = value
            self._save()

    def require_sso_consent(self) -> bool:
        """Return the global opt-in for native authentication confirmations."""
        return self.get(self.REQUIRE_SSO_CONSENT_KEY, False) is True

    def profile_registry(self) -> dict[str, dict[str, Any]]:
        """Return operator-configured profile metadata keyed by stable slug."""
        with self._lock:
            raw = self._data.get(self._PROFILE_REGISTRY_KEY, {})
            return json.loads(json.dumps(raw)) if isinstance(raw, dict) else {}

    def register_profile(
        self,
        slug: str,
        profile_path: Path | str,
        *,
        display_name: str | None = None,
        intended_default_account: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(slug or "").strip().lower()
        if not normalized or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for char in normalized):
            raise ValueError("invalid browser profile slug")
        record = {
            "slug": normalized,
            "path": str(Path(profile_path).expanduser()),
            "display_name": str(display_name or normalized),
            "intended_default_account": (
                str(intended_default_account).strip().lower()
                if intended_default_account
                else None
            ),
        }
        with self._mutation():
            registry = self.profile_registry()
            registry[normalized] = record
            self._data[self._PROFILE_REGISTRY_KEY] = registry
            self._save()
        return json.loads(json.dumps(record))

    def _profiles_copy(self) -> dict[str, Any]:
        raw = self.get(self._PROFILES_KEY, self.get(self._LEGACY_PROFILES_KEY, {}))
        return json.loads(json.dumps(raw)) if isinstance(raw, dict) else {}

    def get_profile_state(self, profile_path: Path | str) -> dict[str, Any]:
        with self._lock:
            self._load()
            key = str(Path(profile_path).expanduser())
            profiles = self._profiles_copy()
            state = profiles.get(key, {})
            if not isinstance(state, dict):
                state = {}
            accounts = state.get("accounts", {})
            role_account_map = state.get("role_account_map", {})
            meeting_role_account_maps = state.get("meeting_role_account_maps", {})
            companion_click = state.get("companion_click", {})
            meeting_companion_click = state.get("meeting_companion_click", {})
            meeting_routing = state.get(self._MEETING_ROUTING_KEY, {})
            test_companion_click = state.get("test_companion_click", {})
            active_test_companion_click = state.get("active_test_companion_click", {})
            known_meeting_urls = state.get("known_meeting_urls", [])
            forgotten_meeting_urls = state.get("forgotten_meeting_urls", [])
            return json.loads(json.dumps({
                "accounts": accounts if isinstance(accounts, dict) else {},
                "role_account_map": role_account_map if isinstance(role_account_map, dict) else {},
                "meeting_role_account_maps": (
                    meeting_role_account_maps if isinstance(meeting_role_account_maps, dict) else {}
                ),
                "companion_click": companion_click if isinstance(companion_click, dict) else {},
                "meeting_companion_click": (
                    meeting_companion_click if isinstance(meeting_companion_click, dict) else {}
                ),
                self._MEETING_ROUTING_KEY: (
                    meeting_routing if isinstance(meeting_routing, dict) else {}
                ),
                "test_companion_click": (
                    test_companion_click if isinstance(test_companion_click, dict) else {}
                ),
                "active_test_companion_click": (
                    active_test_companion_click if isinstance(active_test_companion_click, dict) else {}
                ),
                "known_meeting_urls": known_meeting_urls if isinstance(known_meeting_urls, list) else [],
                "forgotten_meeting_urls": (
                    forgotten_meeting_urls if isinstance(forgotten_meeting_urls, list) else []
                ),
            }))

    def set_profile_state(
        self,
        profile_path: Path | str,
        *,
        accounts: dict[str, Any] | None = None,
        role_account_map: dict[str, Any] | None = None,
        meeting_role_account_maps: dict[str, Any] | None = None,
        companion_click: dict[str, Any] | None = None,
        meeting_companion_click: dict[str, Any] | None = None,
        meeting_routing: dict[str, Any] | None = None,
        test_companion_click: dict[str, Any] | None = None,
        active_test_companion_click: dict[str, Any] | None = None,
        known_meeting_urls: list[str] | None = None,
        forgotten_meeting_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        key = str(Path(profile_path).expanduser())
        with self._mutation():
            profiles = self._profiles_copy()
            state = profiles.get(key, {})
            if not isinstance(state, dict):
                state = {}
            if accounts is not None:
                state["accounts"] = json.loads(json.dumps(accounts))
            if role_account_map is not None:
                state["role_account_map"] = json.loads(json.dumps(role_account_map))
            if meeting_role_account_maps is not None:
                state["meeting_role_account_maps"] = json.loads(json.dumps(meeting_role_account_maps))
            if companion_click is not None:
                state["companion_click"] = json.loads(json.dumps(companion_click))
            if meeting_companion_click is not None:
                state["meeting_companion_click"] = json.loads(json.dumps(meeting_companion_click))
            if meeting_routing is not None:
                state[self._MEETING_ROUTING_KEY] = json.loads(json.dumps(meeting_routing))
            if test_companion_click is not None:
                state["test_companion_click"] = json.loads(json.dumps(test_companion_click))
            if active_test_companion_click is not None:
                state["active_test_companion_click"] = json.loads(json.dumps(active_test_companion_click))
            if known_meeting_urls is not None:
                state["known_meeting_urls"] = json.loads(json.dumps(known_meeting_urls))
            if forgotten_meeting_urls is not None:
                state["forgotten_meeting_urls"] = json.loads(json.dumps(forgotten_meeting_urls))
            profiles[key] = state
            self._data[self._PROFILES_KEY] = profiles
            self._data.pop(self._LEGACY_PROFILES_KEY, None)
            self._save()
        return self.get_profile_state(key)

    def update_profile_state(
        self,
        profile_path: Path | str,
        update: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        """Atomically update one profile after refreshing cross-process changes."""
        key = str(Path(profile_path).expanduser())
        with self._mutation():
            profiles = self._profiles_copy()
            state = profiles.get(key, {})
            state = json.loads(json.dumps(state)) if isinstance(state, dict) else {}
            update(state)
            profiles[key] = state
            self._data[self._PROFILES_KEY] = profiles
            self._data.pop(self._LEGACY_PROFILES_KEY, None)
            self._save()
        return self.get_profile_state(key)

    def unforget_meeting_url(
        self, profile_path: Path | str, meeting_url: str
    ) -> dict[str, Any]:
        """Intentionally restore one explicitly joined channel."""
        key = normalize_meeting_url(meeting_url)

        def update(state: dict[str, Any]) -> None:
            forgotten = {
                normalized
                for value in state.get("forgotten_meeting_urls", [])
                if (normalized := _normalize_optional(value))
            }
            forgotten.discard(key)
            known = [
                normalized
                for value in state.get("known_meeting_urls", [])
                if (normalized := _normalize_optional(value))
                and normalized not in forgotten
            ]
            state["forgotten_meeting_urls"] = sorted(forgotten)
            state["known_meeting_urls"] = list(dict.fromkeys([*known, key]))

        return self.update_profile_state(profile_path, update)

    @staticmethod
    def default_meeting_routing(meeting_url: str) -> dict[str, Any]:
        return {
            "version": 1,
            "meeting_url": normalize_meeting_url(meeting_url),
            "room_adapter": {
                "kind": "physical_computer",
                "id": "physical_computer",
            },
            "roles": {},
            "autostart": False,
            "reconnect_after_disconnect": False,
        }

    def get_meeting_routing(
        self, profile_path: Path | str, meeting_url: str
    ) -> dict[str, Any]:
        key = normalize_meeting_url(meeting_url)
        state = self.get_profile_state(profile_path)
        policies = state.get(self._MEETING_ROUTING_KEY, {})
        saved = policies.get(key, {}) if isinstance(policies, dict) else {}
        result = self.default_meeting_routing(key)
        if isinstance(saved, dict):
            result.update(json.loads(json.dumps(saved)))
        result["meeting_url"] = key
        return result

    def list_meeting_routing(
        self, profile_path: Path | str
    ) -> dict[str, dict[str, Any]]:
        state = self.get_profile_state(profile_path)
        raw = state.get(self._MEETING_ROUTING_KEY, {})
        result: dict[str, dict[str, Any]] = {}
        if not isinstance(raw, dict):
            return result
        for candidate in raw:
            key = _normalize_optional(candidate)
            if key:
                result[key] = self.get_meeting_routing(profile_path, key)
        return result

    def update_meeting_routing(
        self,
        profile_path: Path | str,
        meeting_url: str,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically update one policy and enforce the single-autostart invariant."""

        if not isinstance(patch, dict):
            raise ValueError("meeting routing patch must be an object")
        key = normalize_meeting_url(meeting_url)
        profile_key = str(Path(profile_path).expanduser())
        with self._mutation():
            profiles = self._profiles_copy()
            state = profiles.get(profile_key, {})
            state = json.loads(json.dumps(state)) if isinstance(state, dict) else {}
            forgotten = {
                normalized
                for value in state.get("forgotten_meeting_urls", [])
                if (normalized := _normalize_optional(value))
            }
            if key in forgotten:
                raise ValueError("cannot configure a forgotten meeting")
            raw = state.get(self._MEETING_ROUTING_KEY, {})
            policies = json.loads(json.dumps(raw)) if isinstance(raw, dict) else {}
            current = self.default_meeting_routing(key)
            saved = policies.get(key)
            if isinstance(saved, dict):
                current.update(saved)
            patch_copy = json.loads(json.dumps(patch))
            role_patch = patch_copy.pop("roles", None)
            current.update(patch_copy)
            if role_patch is not None:
                if not isinstance(role_patch, dict):
                    raise ValueError("meeting routing roles patch must be an object")
                roles = current.get("roles", {})
                roles = json.loads(json.dumps(roles)) if isinstance(roles, dict) else {}
                for role, value in role_patch.items():
                    if not isinstance(value, dict):
                        raise ValueError(f"meeting routing role {role!r} must be an object")
                    role_value = roles.get(role, {})
                    role_value = (
                        json.loads(json.dumps(role_value))
                        if isinstance(role_value, dict)
                        else {}
                    )
                    role_value.update(value)
                    roles[role] = role_value
                current["roles"] = roles
            current["version"] = 1
            current["meeting_url"] = key
            if current.get("autostart") is True:
                for other_key, other in policies.items():
                    if other_key == key or not isinstance(other, dict):
                        continue
                    other["autostart"] = False
            policies[key] = current
            state[self._MEETING_ROUTING_KEY] = policies
            known = [
                normalized
                for value in state.get("known_meeting_urls", [])
                if (normalized := _normalize_optional(value))
                and normalized not in forgotten
            ]
            state["known_meeting_urls"] = list(dict.fromkeys([*known, key]))
            profiles[profile_key] = state
            self._data[self._PROFILES_KEY] = profiles
            self._data.pop(self._LEGACY_PROFILES_KEY, None)
            self._save()
        policies = self.list_meeting_routing(profile_key)
        return {
            "policy": policies[key],
            "autostart_meeting": next(
                (
                    url
                    for url in sorted(policies)
                    if policies[url].get("autostart") is True
                ),
                None,
            ),
        }

    def update_meeting_role_accounts(
        self,
        profile_path: Path | str,
        meeting_url: str,
        role_patch: dict[str, str | None],
        *,
        clear: bool = False,
    ) -> dict[str, Any]:
        """Atomically merge only supplied account roles for one meeting."""

        if not isinstance(role_patch, dict):
            raise ValueError("meeting role-account patch must be an object")
        key = normalize_meeting_url(meeting_url)
        profile_key = str(Path(profile_path).expanduser())
        with self._mutation():
            profiles = self._profiles_copy()
            state = profiles.get(profile_key, {})
            state = json.loads(json.dumps(state)) if isinstance(state, dict) else {}
            raw_maps = state.get("meeting_role_account_maps", {})
            maps = json.loads(json.dumps(raw_maps)) if isinstance(raw_maps, dict) else {}
            if clear:
                maps.pop(key, None)
            else:
                raw_overrides = maps.get(key, {})
                overrides = (
                    json.loads(json.dumps(raw_overrides))
                    if isinstance(raw_overrides, dict)
                    else {}
                )
                for role, account_id in role_patch.items():
                    value = str(account_id or "").strip()
                    if value:
                        overrides[str(role)] = value
                    else:
                        overrides.pop(str(role), None)
                if overrides:
                    maps[key] = overrides
                else:
                    maps.pop(key, None)
            state["meeting_role_account_maps"] = maps
            forgotten = {
                normalized
                for value in state.get("forgotten_meeting_urls", [])
                if (normalized := _normalize_optional(value))
            }
            known = [
                normalized
                for value in state.get("known_meeting_urls", [])
                if (normalized := _normalize_optional(value))
                and normalized not in forgotten
            ]
            if key not in forgotten:
                state["known_meeting_urls"] = list(dict.fromkeys([*known, key]))
            profiles[profile_key] = state
            self._data[self._PROFILES_KEY] = profiles
            self._data.pop(self._LEGACY_PROFILES_KEY, None)
            self._save()
        return self.get_profile_state(profile_key)

    def update_role_devices_if_unchanged(
        self,
        profile_path: Path | str,
        meeting_url: str,
        role: str,
        expected: dict[str, Any],
        resolved: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge verified device state only if the role's desired devices are unchanged."""

        key = normalize_meeting_url(meeting_url)
        profile_key = str(Path(profile_path).expanduser())
        with self._mutation():
            profiles = self._profiles_copy()
            state = profiles.get(profile_key, {})
            state = json.loads(json.dumps(state)) if isinstance(state, dict) else {}
            forgotten = {
                normalized
                for value in state.get("forgotten_meeting_urls", [])
                if (normalized := _normalize_optional(value))
            }
            if key in forgotten:
                raise ValueError("cannot configure a forgotten meeting")
            raw = state.get(self._MEETING_ROUTING_KEY, {})
            policies = json.loads(json.dumps(raw)) if isinstance(raw, dict) else {}
            current = self.default_meeting_routing(key)
            saved = policies.get(key)
            if isinstance(saved, dict):
                current.update(saved)
            roles = current.get("roles", {})
            roles = json.loads(json.dumps(roles)) if isinstance(roles, dict) else {}
            role_policy = roles.get(role, {})
            role_policy = (
                json.loads(json.dumps(role_policy))
                if isinstance(role_policy, dict)
                else {}
            )
            current_devices = {
                field: role_policy.get(field) for field in ("mic", "speakers")
            }
            expected_devices = {
                field: expected.get(field) for field in ("mic", "speakers")
            }
            if current_devices != expected_devices:
                return {
                    "updated": False,
                    "policy": current,
                    "current": current_devices,
                }
            for field in ("mic", "speakers"):
                verified = resolved.get(field)
                if not isinstance(verified, dict):
                    continue
                descriptor = role_policy.get(field, {})
                descriptor = dict(descriptor) if isinstance(descriptor, dict) else {}
                descriptor.update(json.loads(json.dumps(verified)))
                role_policy[field] = descriptor
            roles[role] = role_policy
            current["roles"] = roles
            current["version"] = 1
            current["meeting_url"] = key
            policies[key] = current
            state[self._MEETING_ROUTING_KEY] = policies
            profiles[profile_key] = state
            self._data[self._PROFILES_KEY] = profiles
            self._data.pop(self._LEGACY_PROFILES_KEY, None)
            self._save()
            return {"updated": True, "policy": json.loads(json.dumps(current))}

    def clear_profile_state(self, profile_path: Path | str) -> None:
        key = str(Path(profile_path).expanduser())
        with self._mutation():
            profiles = self._profiles_copy()
            profiles.pop(key, None)
            if profiles:
                self._data[self._PROFILES_KEY] = profiles
            else:
                self._data.pop(self._PROFILES_KEY, None)
            self._data.pop(self._LEGACY_PROFILES_KEY, None)
            self._save()


def prune_meeting_channels(
    state_dir: Path | str,
    keep_urls: list[str] | tuple[str, ...] | set[str],
    *,
    profile_path: Path | str | None = None,
    active_meeting_url: str = "",
) -> dict[str, Any]:
    """Safely prune persisted channel state without opening event/JSONL stores."""
    keep = list(dict.fromkeys(normalize_meeting_url(value) for value in keep_urls))
    if not keep:
        raise ValueError("keep_urls must contain at least one Google Meet channel")
    store = MeetBrowserSettings(state_dir)
    profile = Path(
        profile_path or store.get("profile_path") or Path(state_dir) / "meet_bridge_profile"
    ).expanduser()
    active = normalize_meeting_url(active_meeting_url) if active_meeting_url else ""
    if active and active not in keep:
        raise ValueError("cannot forget the currently active meeting unless it is retained")
    before = store.get_profile_state(profile)
    already = {
        normalize_meeting_url(value)
        for value in before.get("forgotten_meeting_urls", [])
        if isinstance(value, str)
        and _MEET_ROOM_RE.fullmatch(value.strip())
    }
    discovered: set[str] = set()
    for value in before.get("known_meeting_urls", []):
        try:
            discovered.add(normalize_meeting_url(value))
        except ValueError:
            pass
    for map_name in (
        "meeting_role_account_maps",
        "meeting_companion_click",
        MeetBrowserSettings._MEETING_ROUTING_KEY,
    ):
        mapping = before.get(map_name, {})
        if isinstance(mapping, dict):
            for value in mapping:
                try:
                    discovered.add(normalize_meeting_url(value))
                except ValueError:
                    pass
    forgotten = sorted((discovered - set(keep)) - already)
    already_forgotten = sorted(already - set(keep))

    def update(state: dict[str, Any]) -> None:
        tombstones = sorted((already | discovered) - set(keep))
        state["known_meeting_urls"] = keep
        state["forgotten_meeting_urls"] = tombstones
        for map_name in (
            "meeting_role_account_maps",
            "meeting_companion_click",
            MeetBrowserSettings._MEETING_ROUTING_KEY,
        ):
            mapping = state.get(map_name, {})
            if isinstance(mapping, dict):
                state[map_name] = {
                    key: value
                    for key, value in mapping.items()
                    if _normalize_optional(key) in keep
                }
        lease = state.get("active_test_companion_click", {})
        if isinstance(lease, dict) and _normalize_optional(lease.get("channelKey")) not in keep:
            state["active_test_companion_click"] = {}

    store.update_profile_state(profile, update)
    from .admin_ui_state import AdminUIState

    AdminUIState(state_dir).clear_page("meet")
    return {
        "kept": keep,
        "forgotten": forgotten,
        "alreadyForgotten": already_forgotten,
        "active": active or None,
        "historyPreserved": True,
    }


def _normalize_optional(value: Any) -> str:
    try:
        return normalize_meeting_url(value)
    except ValueError:
        return ""
