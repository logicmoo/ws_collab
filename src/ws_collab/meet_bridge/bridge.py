"""Two-way Google Meet <-> ws_collab-mailbox bridge using Meet's own live
captions -- orchestration (CLI, loops, status HTTP server).

Local speech recognition is mediocre; Google Meet's caption model is
excellent. This module turns a Meet tab in a dedicated Chrome into the
recognizer -- and a mouthpiece:

  IN   Meet live captions  ->  ws_collab mailbox (one message per finished
       line, sender "meet-<speaker>"), AND exposed at this process's own
       GET /captions HTTP endpoint, which ws_collab's `google_meet` STT
       driver polls and resolves through the normal disambiguator/timeline
       pipeline.
  OUT  ws_collab mailbox messages addressed to the bridge (default mailbox
       "google-meet")  ->  posted into the Meet's in-call chat, and
       optionally spoken aloud with Windows TTS (--speak).

ALWAYS-ON: run with no arguments and the bridge keeps a meeting of its own in
the background as the STT surface -- it signs into the popup browser (SSO
persists), CREATES an instant meeting, and transcribes whoever talks in it.
While running you can point it at any other meeting by MAILBOX COMMAND --
send to the "google-meet" mailbox, or via POST /command on this process's own
status port (what the ws_collab admin UI's Google Meet page uses):

    /join https://meet.google.com/xxx-yyyy-zzz   switch to that meeting
    /new                                          spin up a fresh meeting
    /say <text>                                   speak text into the call
    /click on|off                                 synthetic companion mic tick

(the operator still clicks "Join now" in the popup window; the bridge
re-attaches automatically and posts where it went).

Nothing here logs into Google: you join the meeting normally in a Chrome
window this process pops up with its own remote-debugging port, then the
bridge attaches over the DevTools protocol (CDP) and reads/writes the page.

Two-bot design (task: HOST + COMPANION): Google ends/nags a meeting with a
single silent participant. `--companion` keeps a SECOND signed-in Google
account in the same Chrome profile sitting muted+deaf in the call so
Google always sees two participants; the real (HOST) account's mic is never
touched by any automation.

Setup -- two ways to connect:

  A) Let the bridge POP UP its own browser (default when --meet is given):
       python -m ws_collab.meet_bridge --meet https://meet.google.com/xxx-yyyy-zzz
     A dedicated Chrome window opens (its own persistent profile, so your
     Google SSO login sticks between runs -- you sign in ONCE and the session
     is reused until Google expires it; run with --forget-sso to wipe the
     stored login and pick an account again). Pick your account, join the
     call, turn on captions ("c") -- the bridge waits, attaches, and starts
     bridging automatically.

  B) Attach to a Chrome YOU started:
       chrome.exe --remote-debugging-port=9222
     join the Meet there, then run the bridge with no --meet argument.

For the OUT direction, open the in-call chat panel once (the bridge will try
to open it itself if it can find the button).

Usage (installed as a console script, or `python -m ws_collab.meet_bridge`):
  ws-collab-meet-bridge                       # ALWAYS-ON servant meeting
  ws-collab-meet-bridge --meet <meet-url>     # join a given meet
  ws-collab-meet-bridge --new                 # force-create one
  ws-collab-meet-bridge --attach-only         # never pop a browser
  ws-collab-meet-bridge --list-tabs           # show CDP tabs
  ws-collab-meet-bridge --no-out              # captions only
  ws-collab-meet-bridge --speak               # + local TTS
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import navigator
from .companion_audio import CompanionAudioArbiter
from .audio_out import (
    list_audio_devices,
    play_wav_bytes_to_device,
    resolve_audio_device,
    sapi_wav_base64,
    speak_windows,
)
from .cdp import (
    DEFAULT_CDP,
    DEFAULT_POPUP_PORT,
    DEFAULT_PROFILE,
    DEFAULT_SSO_AUTHUSER_PROBE_SLOTS,
    CdpTab,
    browser_profile_root,
    cdp_alive,
    close_tab,
    configure_browser_nav_logging,
    ensure_default_profile_migrated,
    find_browser,
    find_add_account_tab,
    find_sso_connector_tab,
    launch_browser,
    list_tabs,
    read_google_account,
    reuse_or_open_tab,
    scan_signed_in_sso_accounts,
)
from .mailbox_client import DEFAULT_BASE_URL as DEFAULT_MAILBOX_BASE
from .mailbox_client import BrowserNavIntentPoster, MailboxClient
from .scripts_js import (
    CAPTION_OBSERVER_JS,
    CAPTIONS_JS,
    COMPANION_AUDIO_TAP_JS,
    COMPANION_AUDIO_RMS_JS,
    COMPANION_CLICK_JS,
    COMPANION_CLICK_ONCE_JS,
    CANCEL_COMPANION_AUDIO_JS,
    GUM_PATCH_JS,
    SELECT_MIC_DEVICE_JS,
    SEND_CHAT_JS_TEMPLATE,
    SPEAK_INTO_MEETING_JS,
    autojoin_js,
)
from .tracker import CaptionTracker
from ..meet_browser_settings import MeetBrowserSettings

DEFAULT_RECIPIENTS = ["conversation"]
DEFAULT_SENDER_PREFIX = "meet-"
DEFAULT_OUTBOX = "google-meet"
CAPTION_ROLES = ("host", "companion")
CAPTION_DUPLICATE_WINDOW_SECONDS = 15.0
CAPTION_DUPLICATE_RECENT_LIMIT = 400
CAPTION_PUSH_BINDING = "__wsCollabCaptionPush"
CAPTION_PUSH_HEALTH_SECONDS = 5.0
CAPTION_PUSH_POLL_INTERVAL = 2.0
CAPTION_PUSH_MAX_PAYLOAD_BYTES = 256 * 1024
CAPTION_PUSH_REINSTALL_SECONDS = 10.0
_CAPTION_PUSH_ERROR_LOG_AT: dict[tuple[str, str], float] = {}
_CAPTION_PUSH_LIFECYCLE_LOG_AT: dict[tuple[str, str], float] = {}
_CAPTION_PUSH_INSTALL_ATTEMPT_LOGGED: set[str] = set()
_CAPTION_PUSH_FIRST_FRAME_LOGGED: set[str] = set()
_COMPANION_CLICK_LOG_AT: dict[str, float] = {}

# Meet's own room-id shape ("xxx-yyyy-zzz") -- the stable identity used to key
# per-room state (meeting_state below) regardless of whether that room is a
# HOST+COMPANION driver/servant meeting or (once built) a CLIENT/GUEST
# meeting the bridge just sits in: both are keyed the same way, uniformly.
_ROOM_RE = re.compile(r"meet\.google\.com/([a-z]{3,4}-[a-z]{3,5}-[a-z]{3,4})", re.IGNORECASE)
_ROOM_ID_RE = re.compile(r"^[a-z]{3,4}-[a-z]{3,5}-[a-z]{3,4}$", re.IGNORECASE)
_CAPTION_SPACE_RE = re.compile(r"\s+")


def read_sso_consent_setting(settings_dir: Path | str) -> bool:
    """Read the atomic shared setting afresh for each authentication navigation."""
    return MeetBrowserSettings(settings_dir).require_sso_consent()


def room_id(url: str | None) -> str | None:
    match = _ROOM_RE.search(url or "")
    return match.group(1).lower() if match else None


def meeting_key(value: str | None) -> str | None:
    text = str(value or "").strip()
    room = room_id(text)
    if room is None and _ROOM_ID_RE.fullmatch(text):
        room = text.lower()
    return f"https://meet.google.com/{room}" if room else None


def _blank_raw_caption_role() -> dict[str, Any]:
    return {
        "rawText": "",
        "rawRows": [],
        "rawAt": None,
        "rawIso": None,
        "rawRowCount": 0,
        "rawChildCount": 0,
        "rawHistory": [],
        "rawHistoryCount": 0,
    }


def _ensure_raw_by_role(target: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_by_role = target.setdefault("rawByRole", {})
    if not isinstance(raw_by_role, dict):
        raw_by_role = {}
        target["rawByRole"] = raw_by_role
    for role in CAPTION_ROLES:
        if not isinstance(raw_by_role.get(role), dict):
            raw_by_role[role] = _blank_raw_caption_role()
    return raw_by_role


def _blank_caption_transport_role() -> dict[str, Any]:
    return {
        "captionTransport": "poll",
        "lastPushAt": None,
        "lastPushIso": None,
        "pushFrameCount": 0,
    }


def _ensure_caption_transport_by_role(target: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_role = target.setdefault("captionTransportByRole", {})
    if not isinstance(by_role, dict):
        by_role = {}
        target["captionTransportByRole"] = by_role
    for role in CAPTION_ROLES:
        state = by_role.get(role)
        if not isinstance(state, dict):
            by_role[role] = _blank_caption_transport_role()
            continue
        for key, value in _blank_caption_transport_role().items():
            state.setdefault(key, value)
    return by_role


def _refresh_caption_transport_state(
    status: dict[str, Any],
    *,
    active_roles: list[str] | tuple[str, ...] | None = None,
    now: float | None = None,
    log: Callable[..., None] | None = None,
) -> dict[str, dict[str, Any]]:
    at = time.time() if now is None else now
    by_role = _ensure_caption_transport_by_role(status)
    for role, state in by_role.items():
        last_push = state.get("lastPushAt")
        mode = "push" if isinstance(last_push, (int, float)) and at - float(last_push) <= CAPTION_PUSH_HEALTH_SECONDS else "poll"
        previous = state.get("captionTransport")
        if previous in {"poll", "push"} and previous != mode:
            _log_caption_push_lifecycle(log, role, "transport", f"[captions] transport {previous}->{mode}")
        state["captionTransport"] = mode
    primary_role = "host"
    if active_roles:
        primary_role = next((role for role in active_roles if role in by_role), "host")
    primary = by_role.get(primary_role) or _blank_caption_transport_role()
    status["captionTransport"] = primary.get("captionTransport", "poll")
    status["lastPushAt"] = primary.get("lastPushAt")
    status["lastPushIso"] = primary.get("lastPushIso")
    status["pushFrameCount"] = primary.get("pushFrameCount", 0)
    status["captionTransportByRole"] = by_role
    return by_role


def _mark_caption_push(
    status: dict[str, Any],
    role: str,
    *,
    now: float | None = None,
    log: Callable[..., None] | None = None,
) -> None:
    role = role if role in CAPTION_ROLES else "host"
    at = time.time() if now is None else now
    by_role = _ensure_caption_transport_by_role(status)
    state = by_role[role]
    previous = state.get("captionTransport")
    if previous in {"poll", "push"} and previous != "push":
        _log_caption_push_lifecycle(log, role, "transport", "[captions] transport poll->push")
    state["captionTransport"] = "push"
    state["lastPushAt"] = at
    state["lastPushIso"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at))
    state["pushFrameCount"] = int(state.get("pushFrameCount") or 0) + 1
    _refresh_caption_transport_state(status, now=at, log=log)


def _log_caption_push_lifecycle(
    log: Callable[..., None] | None,
    role: str,
    key: str,
    message: str,
    *,
    interval: float = 60.0,
) -> None:
    if log is None:
        return
    now = time.monotonic()
    log_key = (role, key)
    if interval > 0 and now - _CAPTION_PUSH_LIFECYCLE_LOG_AT.get(log_key, 0.0) < interval:
        return
    _CAPTION_PUSH_LIFECYCLE_LOG_AT[log_key] = now
    log(message, role=role)


def _log_caption_push_error(
    log: Callable[..., None] | None,
    role: str,
    key: str,
    message: str,
    *,
    interval: float = 5.0,
) -> None:
    if log is None:
        return
    now = time.monotonic()
    log_key = (role, key)
    if now - _CAPTION_PUSH_ERROR_LOG_AT.get(log_key, 0.0) < interval:
        return
    _CAPTION_PUSH_ERROR_LOG_AT[log_key] = now
    log(message, err=True, role=role)


def _log_companion_click(
    log: Callable[..., None] | None,
    key: str,
    message: str,
    *,
    err: bool = False,
    interval: float = 60.0,
) -> None:
    if log is None:
        return
    now = time.monotonic()
    if interval > 0 and now - _COMPANION_CLICK_LOG_AT.get(key, 0.0) < interval:
        return
    _COMPANION_CLICK_LOG_AT[key] = now
    log(message, err=err, role="companion")


def companion_click_interval(value: Any) -> float:
    try:
        interval = float(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("expected seconds as a positive number") from error
    if interval <= 0:
        raise argparse.ArgumentTypeError("expected seconds as a positive number")
    return interval


def companion_click_positive_float(value: Any) -> float:
    return companion_click_interval(value)


def companion_click_nonnegative_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("expected seconds as a non-negative number") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("expected seconds as a non-negative number")
    return parsed


def companion_click_ms(value: Any) -> float:
    ms = companion_click_interval(value)
    if ms > 1000:
        raise argparse.ArgumentTypeError("expected milliseconds <= 1000")
    return ms


def companion_click_gain(value: Any) -> float:
    try:
        gain = float(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("expected gain as a positive number") from error
    if gain <= 0 or gain > 1:
        raise argparse.ArgumentTypeError("expected gain in (0, 1]")
    return gain


def parse_companion_click_command(command: str) -> dict[str, Any] | None:
    parts = str(command or "").strip().split()
    if not parts or parts[0].lower() != "/click":
        return None
    if len(parts) == 1 or parts[1].lower() in {"status", "?"}:
        target = parts[2] if len(parts) > 2 else None
        if len(parts) > 3:
            return {"action": "invalid", "error": "too many /click status arguments"}
        if target and meeting_key(target) is None:
            return {"action": "invalid", "error": "expected a Meet URL or room id after /click status"}
        return {"action": "status", "meetingUrl": meeting_key(target) if target else None}
    action = parts[1].lower()
    if action in {"on", "start", "enable", "enabled"}:
        interval = None
        target = None
        remaining = parts[2:]
        if remaining and meeting_key(remaining[0]):
            target = meeting_key(remaining.pop(0))
        if remaining:
            try:
                interval = companion_click_interval(remaining[0])
            except argparse.ArgumentTypeError as error:
                return {"action": "invalid", "error": str(error)}
        if len(remaining) > 1:
            return {"action": "invalid", "error": "too many /click on arguments"}
        return {"action": "on", "intervalSeconds": interval, "meetingUrl": target}
    if action in {"off", "stop", "disable", "disabled"}:
        target = meeting_key(parts[2]) if len(parts) > 2 else None
        if len(parts) > 2 and target is None:
            return {"action": "invalid", "error": "expected a Meet URL or room id after /click off"}
        if len(parts) > 3:
            return {"action": "invalid", "error": "too many /click off arguments"}
        return {"action": "off", "meetingUrl": target}
    return {"action": "invalid", "error": "expected /click on [meeting] [seconds], /click off [meeting], or /click status [meeting]"}


def update_companion_click_status(status: dict[str, Any], holder: dict[str, Any]) -> dict[str, Any]:
    last_click_at = holder.get("companion_click_last_click_at")
    if isinstance(last_click_at, (int, float)):
        last_click_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(last_click_at)))
    else:
        last_click_at = None
        last_click_iso = None
    payload = {
        "enabled": bool(holder.get("companion_click_enabled")),
        "intervalSeconds": float(holder.get("companion_click_interval_seconds") or 2.0),
        "lastClickAt": last_click_at,
        "lastClickIso": last_click_iso,
        "meetingUrl": holder.get("companion_click_meeting_url"),
        "source": str(holder.get("companion_click_source") or "default"),
        "mode": str(holder.get("companion_click_mode") or "reactive"),
        "triggerMode": "interval" if holder.get("companion_click_mode") == "fixed" else "on_silence",
        "trigger": str(holder.get("companion_click_trigger") or "caption"),
        "afterSeconds": float(holder.get("companion_click_after_seconds") or 10.0),
        "silenceMs": float(holder.get("companion_click_silence_ms") or 500.0),
        "minGapSeconds": float(holder.get("companion_click_min_gap_seconds") or 6.0),
        "maxWaitSeconds": float(holder.get("companion_click_max_wait_seconds") or 0.0),
        "clickMs": float(holder.get("companion_click_ms") or 100.0),
        "gain": float(holder.get("companion_click_gain") or 0.12),
        "sound": str(holder.get("companion_click_sound") or "uh"),
        "phrase": str(holder.get("companion_click_phrase") or holder.get("companion_click_sound") or "uh"),
        "f0Hz": float(holder.get("companion_click_f0_hz") or 125.0),
        "f1Hz": float(holder.get("companion_click_f1_hz") or 600.0),
        "f2Hz": float(holder.get("companion_click_f2_hz") or 1300.0),
        "clicksSent": int(holder.get("companion_clicks_sent") or 0),
        "suppressed": int(holder.get("companion_click_suppressed") or 0),
        "rowBreaksObserved": int(holder.get("companion_click_row_breaks_observed") or 0),
        "lastTrigger": holder.get("companion_click_last_trigger"),
        "lastTriggerMode": holder.get("companion_click_last_trigger_mode"),
        "lastTriggerReason": holder.get("companion_click_last_trigger_reason"),
        "lastTriggerAt": holder.get("companion_click_last_trigger_at"),
        "lastTriggerIso": holder.get("companion_click_last_trigger_iso"),
        "lastPhrase": holder.get("companion_click_last_phrase"),
        "currentSilenceMs": holder.get("companion_click_current_silence_ms"),
        "eligibility": holder.get("companion_click_eligibility"),
        "companionReady": bool(holder.get("companion_click_companion_ready")),
        "queue": holder.get("companion_click_queue") or {
            "queued": 0,
            "speaking": False,
            "currentKind": None,
        },
        "lastSilenceMs": holder.get("companion_click_last_silence_ms"),
        "lastMonologueSeconds": holder.get("companion_click_last_monologue_seconds"),
        "audioRms": holder.get("companion_click_audio_rms"),
        "audioQuietMs": holder.get("companion_click_audio_quiet_ms"),
        "audioRmsThreshold": float(holder.get("companion_click_audio_rms_threshold") or 0.015),
        "audioStatus": holder.get("companion_click_audio_status"),
        "installed": bool(holder.get("companion_click_installed")),
        "lastInstallAt": holder.get("companion_click_last_install_at"),
        "lastError": holder.get("companion_click_last_error"),
    }
    status["companionClick"] = payload
    return payload


def update_companion_heard_stt_status(status: dict[str, Any], holder: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "enabled": bool(holder.get("companion_heard_stt_enabled")),
        "sourceKind": "companion_heard",
        "audioSource": "companion_heard_meeting_audio",
        "captureMode": "muted-remote-media-stream",
        "tapStatus": holder.get("companion_heard_stt_tap_status"),
        "tapConnected": bool(holder.get("companion_heard_stt_tap_connected")),
        "mediaElementsMuted": bool(holder.get("companion_heard_stt_media_muted", True)),
        "streamId": holder.get("companion_heard_stt_stream_id"),
        "sampleRate": holder.get("companion_heard_stt_sample_rate"),
        "chunksCaptured": int(holder.get("companion_heard_stt_chunks_captured") or 0),
        "framesCaptured": int(holder.get("companion_heard_stt_frames_captured") or 0),
        "bytesCaptured": int(holder.get("companion_heard_stt_bytes_captured") or 0),
        "chunksForwarded": int(holder.get("companion_heard_stt_chunks_forwarded") or 0),
        "framesForwarded": int(holder.get("companion_heard_stt_frames_forwarded") or 0),
        "bytesForwarded": int(holder.get("companion_heard_stt_bytes_forwarded") or 0),
        "chunksDropped": int(holder.get("companion_heard_stt_chunks_dropped") or 0) + int(holder.get("companion_heard_stt_transport_chunks_dropped") or 0),
        "framesDropped": int(holder.get("companion_heard_stt_frames_dropped") or 0) + int(holder.get("companion_heard_stt_transport_frames_dropped") or 0),
        "bytesDropped": int(holder.get("companion_heard_stt_bytes_dropped") or 0) + int(holder.get("companion_heard_stt_transport_bytes_dropped") or 0),
        "transportChunksDropped": int(holder.get("companion_heard_stt_transport_chunks_dropped") or 0),
        "artifactChunksSuppressed": int(holder.get("companion_heard_stt_artifact_chunks_suppressed") or 0),
        "lastSuppressionArtifact": holder.get("companion_say_artifact"),
        "disconnects": int(holder.get("companion_heard_stt_disconnects") or 0),
        "reconnects": int(holder.get("companion_heard_stt_reconnects") or 0),
        "serverCapture": holder.get("companion_heard_stt_server_capture"),
        "outputDeviceSelector": str(holder.get("companion_heard_stt_output_device") or ""),
        "inputDeviceSelector": str(holder.get("companion_heard_stt_input_device_selector") or ""),
        "inputDeviceId": holder.get("companion_heard_stt_input_device_id"),
        "inputDeviceName": holder.get("companion_heard_stt_input_device_name"),
        "captureListening": bool(holder.get("companion_heard_stt_capture_listening")),
        "captureLive": bool(holder.get("companion_heard_stt_capture_live")),
        "lastCaptureAttemptAt": holder.get("companion_heard_stt_capture_last_attempt_at"),
        "sinkStatus": holder.get("companion_heard_stt_sink_status"),
        "sinkDeviceLabel": holder.get("companion_heard_stt_sink_device_label"),
        "lastError": holder.get("companion_heard_stt_last_error"),
        "selfAudioExclusion": "only remote media-element MediaStreams are tapped; synthetic mic is not in that graph; /say and click artifact windows are dropped",
        "engineScope": "server secondary capture excludes google_meet and feeds non-Meet STT engines",
    }
    status["companionHeardStt"] = payload
    return payload


def forward_companion_heard_audio(
    tab: Any,
    mailbox: Any,
    holder: dict[str, Any],
    status: dict[str, Any],
    *,
    log: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Drain muted remote-stream PCM from the companion tab into secondary capture."""

    try:
        raw = tab.evaluate(COMPANION_AUDIO_TAP_JS, await_promise=True, timeout=5)
        tap = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        if not isinstance(tap, dict):
            raise ValueError("companion audio tap returned a non-object payload")
    except Exception as error:  # noqa: BLE001
        tap = {
            "ok": False,
            "status": "tap-error",
            "connected": False,
            "muted": True,
            "chunks": [],
            "lastError": str(error),
        }

    holder["companion_heard_stt_tap_status"] = tap.get("status")
    holder["companion_heard_stt_tap_connected"] = bool(tap.get("connected"))
    holder["companion_heard_stt_media_muted"] = bool(tap.get("muted"))
    holder["companion_heard_stt_stream_id"] = tap.get("streamId")
    holder["companion_heard_stt_sample_rate"] = tap.get("sampleRate") or holder.get("companion_heard_stt_sample_rate") or 48000
    for source, target in (
        ("capturedChunks", "companion_heard_stt_chunks_captured"),
        ("capturedFrames", "companion_heard_stt_frames_captured"),
        ("capturedBytes", "companion_heard_stt_bytes_captured"),
        ("droppedChunks", "companion_heard_stt_chunks_dropped"),
        ("droppedFrames", "companion_heard_stt_frames_dropped"),
        ("droppedBytes", "companion_heard_stt_bytes_dropped"),
        ("disconnects", "companion_heard_stt_disconnects"),
        ("reconnects", "companion_heard_stt_reconnects"),
    ):
        if isinstance(tap.get(source), (int, float)):
            current = int(tap[source])
            raw_key = f"{target}_raw"
            previous = int(holder.get(raw_key) or 0)
            holder[target] = int(holder.get(target) or 0) + (current - previous if current >= previous else current)
            holder[raw_key] = current

    chunks = list(tap.get("chunks") or [])
    click_until = float(holder.get("companion_click_artifact_until") or 0.0)
    say_artifact_until = float(holder.get("companion_say_artifact_until") or 0.0)
    artifact_ranges = [
        (max(0.0, click_until - 2.5) * 1000.0, click_until * 1000.0),
        (
            float(holder.get("companion_say_artifact_started_at") or 0.0) * 1000.0,
            say_artifact_until * 1000.0,
        ),
    ]
    safe_chunks: list[dict[str, Any]] = []
    suppressed_chunks = 0
    suppressed_frames = 0
    suppressed_bytes = 0
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        captured_at = float(chunk.get("capturedAt") or 0.0)
        is_artifact = captured_at > 0 and any(
            end > 0 and start <= captured_at <= end
            for start, end in artifact_ranges
        )
        if is_artifact or not tap.get("muted", True):
            suppressed_chunks += 1
            suppressed_frames += int(chunk.get("frames") or 0)
            suppressed_bytes += int(chunk.get("bytes") or 0)
        else:
            safe_chunks.append(chunk)
    holder["companion_heard_stt_artifact_chunks_suppressed"] = (
        int(holder.get("companion_heard_stt_artifact_chunks_suppressed") or 0) + suppressed_chunks
    )

    request = {
        "stream_id": str(tap.get("streamId") or "companion-remote-media"),
        "sample_rate": int(holder["companion_heard_stt_sample_rate"]),
        "channels": 1,
        "connected": bool(tap.get("connected")),
        "muted": bool(tap.get("muted", True)),
        "chunks": safe_chunks,
        "suppressed_artifact_chunks": suppressed_chunks,
        "suppressed_artifact_frames": suppressed_frames,
        "suppressed_artifact_bytes": suppressed_bytes,
        "suppression_artifact": (
            dict(holder.get("companion_say_artifact") or {})
            if suppressed_chunks and holder.get("companion_say_artifact")
            else None
        ),
    }
    try:
        capture = mailbox.ingest_companion_browser_audio(request)
        holder["companion_heard_stt_server_capture"] = capture
        holder["companion_heard_stt_capture_listening"] = bool(capture.get("listening"))
        holder["companion_heard_stt_capture_live"] = bool(capture.get("live_capture"))
        holder["companion_heard_stt_chunks_forwarded"] = int(capture.get("chunks_forwarded") or 0)
        holder["companion_heard_stt_frames_forwarded"] = int(capture.get("frames_forwarded") or 0)
        holder["companion_heard_stt_bytes_forwarded"] = int(capture.get("bytes_forwarded") or 0)
        holder["companion_heard_stt_last_error"] = capture.get("error") or tap.get("lastError")
    except Exception as error:  # noqa: BLE001
        holder["companion_heard_stt_capture_live"] = False
        holder["companion_heard_stt_last_error"] = str(error)
        holder["companion_heard_stt_transport_chunks_dropped"] = (
            int(holder.get("companion_heard_stt_transport_chunks_dropped") or 0) + len(safe_chunks)
        )
        holder["companion_heard_stt_transport_frames_dropped"] = (
            int(holder.get("companion_heard_stt_transport_frames_dropped") or 0)
            + sum(int(chunk.get("frames") or 0) for chunk in safe_chunks)
        )
        holder["companion_heard_stt_transport_bytes_dropped"] = (
            int(holder.get("companion_heard_stt_transport_bytes_dropped") or 0)
            + sum(int(chunk.get("bytes") or 0) for chunk in safe_chunks)
        )
        if log:
            _log_companion_click(
                log,
                "companion-audio-forward",
                f"[companion-audio] muted remote-stream forwarding unavailable: {error}",
                err=True,
                interval=30.0,
            )
    return update_companion_heard_stt_status(status, holder)


def apply_companion_click_state(
    tab: Any,
    holder: dict[str, Any],
    status: dict[str, Any],
    *,
    log: Callable[..., None] | None = None,
) -> bool:
    enabled = bool(holder.get("companion_click_enabled"))
    interval_seconds = float(holder.get("companion_click_interval_seconds") or 2.0)
    interval_ms = max(1, int(round(interval_seconds * 1000)))
    duration_seconds = max(0.001, float(holder.get("companion_click_ms") or 100.0) / 1000.0)
    gain = max(0.0, min(1.0, float(holder.get("companion_click_gain") or 0.12)))
    # Fixed and reactive interjects are both scheduled by the Python arbiter so
    # they share one serialized outbound track with virtual-agent speech.
    fixed_interval = False
    sound = str(holder.get("companion_click_phrase") or holder.get("companion_click_sound") or "uh").lower()
    if sound not in {"uh", "uhuh", "hmm", "click"}:
        sound = "uh"
    f0 = max(1.0, float(holder.get("companion_click_f0_hz") or 125.0))
    f1 = max(1.0, float(holder.get("companion_click_f1_hz") or 600.0))
    f2 = max(1.0, float(holder.get("companion_click_f2_hz") or 1300.0))
    try:
        raw = tab.evaluate(
            COMPANION_CLICK_JS % (
                "true" if enabled else "false",
                interval_ms,
                json.dumps(duration_seconds),
                json.dumps(gain),
                "true" if fixed_interval else "false",
                json.dumps(sound),
                json.dumps(f0),
                json.dumps(f1),
                json.dumps(f2),
            ),
            timeout=5,
        )
        payload = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        js_enabled = bool(payload.get("enabled"))
        holder["companion_click_installed"] = js_enabled
        holder["companion_click_last_install_at"] = time.time() if js_enabled else holder.get("companion_click_last_install_at")
        last_click_ms = payload.get("lastClickAt")
        if isinstance(last_click_ms, (int, float)) and last_click_ms > 0:
            holder["companion_click_last_click_at"] = float(last_click_ms) / 1000.0
        click_count = payload.get("clickCount")
        if isinstance(click_count, (int, float)):
            previous_count = int(holder.get("companion_click_js_click_count") or 0)
            next_count = int(click_count)
            if next_count > previous_count:
                delta = next_count - previous_count
                holder["companion_clicks_sent"] = int(holder.get("companion_clicks_sent") or 0) + delta
                holder["companion_click_last_trigger"] = "fixed-interval"
                holder["companion_click_last_silence_ms"] = None
                holder["companion_click_last_monologue_seconds"] = None
                holder["companion_click_artifact_until"] = time.time() + 2.5
                pending = list(holder.get("companion_click_pending_breaks") or [])
                for _ in range(delta):
                    pending.append({"at": time.time(), "priorHostKey": holder.get("host_active_caption_key"), "observed": False})
                holder["companion_click_pending_breaks"] = pending[-20:]
            holder["companion_click_js_click_count"] = next_count
        holder["companion_click_last_error"] = payload.get("lastError")
        update_companion_click_status(status, holder)
        update_companion_heard_stt_status(status, holder)
        js_status = str(payload.get("status") or "")
        if enabled and js_status in {"installed", "reinstalled"}:
            _log_companion_click(log, "install", f"[click] companion ticker {js_status} ({interval_seconds:g}s)", interval=0.0)
        if enabled and payload.get("lastError"):
            _log_companion_click(log, "js-error", f"[click] companion ticker waiting: {payload.get('lastError')}", err=True, interval=10.0)
        return True
    except Exception as error:  # noqa: BLE001
        holder["companion_click_installed"] = False
        holder["companion_click_last_error"] = str(error)
        update_companion_click_status(status, holder)
        _log_companion_click(log, "install-failed", f"[click] companion ticker unavailable ({error})", err=True, interval=10.0)
        return False


def trigger_companion_click(
    tab: Any,
    holder: dict[str, Any],
    status: dict[str, Any],
    *,
    prior_host_key: str | None = None,
    trigger_reason: str | None = None,
    silence_ms: float | None = None,
    monologue_seconds: float | None = None,
    phrase: str | None = None,
    log: Callable[..., None] | None = None,
) -> bool:
    try:
        raw = tab.evaluate(COMPANION_CLICK_ONCE_JS, await_promise=True, timeout=5)
        payload = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        if not payload.get("ok"):
            holder["companion_click_last_error"] = payload.get("lastError") or payload.get("status") or "click failed"
            update_companion_click_status(status, holder)
            _log_companion_click(log, "trigger-failed", f"[click] companion click failed: {holder['companion_click_last_error']}", err=True, interval=10.0)
            return False
        last_click_ms = payload.get("lastClickAt")
        now = time.time()
        if isinstance(last_click_ms, (int, float)) and last_click_ms > 0:
            now = float(last_click_ms) / 1000.0
        holder["companion_click_last_click_at"] = now
        holder["companion_click_last_error"] = None
        holder["companion_clicks_sent"] = int(holder.get("companion_clicks_sent") or 0) + 1
        if isinstance(payload.get("clickCount"), (int, float)):
            holder["companion_click_js_click_count"] = int(payload.get("clickCount"))
        holder["companion_click_last_trigger"] = trigger_reason
        holder["companion_click_last_trigger_reason"] = trigger_reason
        holder["companion_click_last_trigger_mode"] = (
            "interval" if holder.get("companion_click_mode") == "fixed" else "on_silence"
        )
        holder["companion_click_last_trigger_at"] = now
        holder["companion_click_last_trigger_iso"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(now)
        )
        holder["companion_click_last_phrase"] = str(
            phrase or holder.get("companion_click_phrase") or holder.get("companion_click_sound") or "uh"
        )
        holder["companion_click_last_silence_ms"] = silence_ms
        holder["companion_click_last_monologue_seconds"] = monologue_seconds
        holder["companion_click_artifact_until"] = time.time() + 2.5
        pending = list(holder.get("companion_click_pending_breaks") or [])
        pending.append({"at": time.time(), "priorHostKey": prior_host_key, "observed": False})
        holder["companion_click_pending_breaks"] = pending[-20:]
        update_companion_click_status(status, holder)
        return True
    except Exception as error:  # noqa: BLE001
        holder["companion_click_last_error"] = str(error)
        update_companion_click_status(status, holder)
        _log_companion_click(log, "trigger-exception", f"[click] companion click failed: {error}", err=True, interval=10.0)
        return False


def measure_companion_audio_silence(
    tab: Any,
    holder: dict[str, Any],
    status: dict[str, Any],
    *,
    log: Callable[..., None] | None = None,
) -> dict[str, Any]:
    threshold = max(0.0, float(holder.get("companion_click_audio_rms_threshold") or 0.015))
    try:
        raw = tab.evaluate(COMPANION_AUDIO_RMS_JS % json.dumps(threshold), await_promise=True, timeout=2)
        payload = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except Exception as error:  # noqa: BLE001
        payload = {"ok": False, "status": "audio-unavailable", "lastError": str(error)}
    holder["companion_click_audio_status"] = payload.get("status")
    if isinstance(payload.get("rms"), (int, float)):
        holder["companion_click_audio_rms"] = float(payload["rms"])
    if isinstance(payload.get("quietMs"), (int, float)):
        holder["companion_click_audio_quiet_ms"] = float(payload["quietMs"])
    if not payload.get("ok") and payload.get("lastError"):
        _log_companion_click(log, "audio-rms", f"[click] audio-rms silence detector unavailable: {payload.get('lastError')}", err=True, interval=30.0)
    update_companion_click_status(status, holder)
    return payload


def companion_click_trigger_decision(
    holder: dict[str, Any],
    *,
    now: float | None = None,
    audio_probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    at = time.monotonic() if now is None else now
    row_key = holder.get("host_active_caption_key")
    started_at = holder.get("host_active_caption_started_at")
    last_growth_at = holder.get("host_active_caption_last_growth_at")
    if not row_key or not isinstance(started_at, (int, float)) or not isinstance(last_growth_at, (int, float)):
        return {"due": False, "reason": "no-active-host-row", "rowKey": row_key}
    monologue_seconds = max(0.0, at - float(started_at))
    caption_silence_ms = max(0.0, (at - float(last_growth_at)) * 1000.0)
    silence_ms = float(holder.get("companion_click_silence_ms") or 500.0)
    after_seconds = float(holder.get("companion_click_after_seconds") or 10.0)
    min_gap = float(holder.get("companion_click_min_gap_seconds") or 6.0)
    max_wait = max(0.0, float(holder.get("companion_click_max_wait_seconds") or 0.0))
    last_sent = float(holder.get("companion_click_last_trigger_monotonic") or 0.0)
    if monologue_seconds < after_seconds:
        return {
            "due": False,
            "reason": "monologue-gate",
            "rowKey": row_key,
            "silenceMs": caption_silence_ms,
            "monologueSeconds": monologue_seconds,
        }
    if at - last_sent < min_gap:
        return {
            "due": False,
            "reason": "min-gap",
            "rowKey": row_key,
            "silenceMs": caption_silence_ms,
            "monologueSeconds": monologue_seconds,
        }
    trigger_source = str(holder.get("companion_click_trigger") or "caption").lower()
    if trigger_source not in {"caption", "audio", "both"}:
        trigger_source = "caption"
    caption_quiet = caption_silence_ms >= silence_ms
    if trigger_source in {"audio", "both"} and caption_silence_ms < min(250.0, silence_ms):
        return {
            "due": False,
            "reason": "caption-mid-growth",
            "rowKey": row_key,
            "silenceMs": caption_silence_ms,
            "monologueSeconds": monologue_seconds,
        }
    audio_quiet_ms = 0.0
    audio_quiet = False
    if audio_probe and audio_probe.get("ok") and isinstance(audio_probe.get("quietMs"), (int, float)):
        audio_quiet_ms = max(0.0, float(audio_probe["quietMs"]))
        audio_quiet = audio_quiet_ms >= silence_ms
    if trigger_source == "caption":
        due = caption_quiet
        trigger = "caption-stasis"
        chosen_silence_ms = caption_silence_ms
    elif trigger_source == "audio":
        due = audio_quiet
        trigger = "audio-rms"
        chosen_silence_ms = audio_quiet_ms
    else:
        due = caption_quiet and audio_quiet
        trigger = "caption-stasis+audio-rms"
        chosen_silence_ms = min(caption_silence_ms, audio_quiet_ms)
    if max_wait > 0 and monologue_seconds >= max_wait:
        due = True
        trigger = "max-wait"
        chosen_silence_ms = caption_silence_ms
    event_key = f"{row_key}:{float(last_growth_at):.6f}"
    if due and holder.get("companion_click_fired_silence_event") == event_key:
        due = False
        trigger = "continuous-silence-debounced"
    elif not due and trigger_source in {"audio", "both"} and not audio_quiet:
        holder.pop("companion_click_fired_silence_event", None)
    return {
        "due": due,
        "trigger": trigger if due or trigger == "continuous-silence-debounced" else "waiting-for-silence",
        "eventKey": event_key,
        "rowKey": row_key,
        "silenceMs": chosen_silence_ms,
        "captionSilenceMs": caption_silence_ms,
        "audioSilenceMs": audio_quiet_ms,
        "monologueSeconds": monologue_seconds,
    }


def companion_interjection_decision(
    holder: dict[str, Any],
    *,
    now: float | None = None,
    audio_probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one scheduler decision for either supported operator mode."""

    at = time.monotonic() if now is None else now
    mode = str(holder.get("companion_click_mode") or "reactive").lower()
    if mode == "fixed":
        interval = max(0.1, float(holder.get("companion_click_interval_seconds") or 2.0))
        anchor = holder.get("companion_click_last_trigger_monotonic")
        minimum_gap = max(0.1, float(holder.get("companion_click_min_gap_seconds") or 6.0))
        wait_seconds = max(interval, minimum_gap) if isinstance(anchor, (int, float)) else interval
        if not isinstance(anchor, (int, float)):
            anchor = holder.get("companion_click_schedule_started_monotonic")
        if not isinstance(anchor, (int, float)):
            holder["companion_click_schedule_started_monotonic"] = at
            anchor = at
        elapsed = max(0.0, at - float(anchor))
        return {
            "due": elapsed >= wait_seconds,
            "trigger": "interval-elapsed" if elapsed >= wait_seconds else "interval-wait",
            "mode": "interval",
            "rowKey": holder.get("host_active_caption_key"),
            "silenceMs": None,
            "monologueSeconds": None,
            "elapsedSeconds": elapsed,
            "waitSeconds": wait_seconds,
        }
    decision = companion_click_trigger_decision(holder, now=at, audio_probe=audio_probe)
    decision["mode"] = "on_silence"
    return decision


def mark_companion_interjection_queued(
    holder: dict[str, Any],
    decision: dict[str, Any],
    *,
    now: float | None = None,
) -> None:
    at = time.monotonic() if now is None else now
    holder["companion_click_last_trigger_monotonic"] = at
    if decision.get("mode") == "on_silence" and decision.get("eventKey"):
        holder["companion_click_fired_silence_event"] = decision["eventKey"]


def queue_companion_interjection(
    holder: dict[str, Any],
    companion_audio: Any,
    decision: dict[str, Any],
    *,
    meeting_url: str | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Apply readiness/serialization gates and enqueue at most one backchannel."""

    at = time.monotonic() if now is None else now
    outbound = companion_audio.status()
    holder["companion_click_companion_ready"] = bool(outbound.get("companionReady"))
    holder["companion_click_queue"] = {
        "queued": int(outbound.get("queued") or 0),
        "speaking": bool(outbound.get("speaking")),
        "currentKind": (outbound.get("current") or {}).get("kind"),
    }
    holder["companion_click_current_silence_ms"] = decision.get("silenceMs")
    if not decision.get("due"):
        reason = str(decision.get("reason") or decision.get("trigger") or "waiting")
        holder["companion_click_eligibility"] = reason
        return {"accepted": False, "reason": reason}
    if not outbound.get("companionReady"):
        reason = "companion-not-ready"
    elif outbound.get("speaking") or outbound.get("queued"):
        reason = (
            "agent-speech-active"
            if (outbound.get("current") or {}).get("kind") == "speech"
            else "output-queue-busy"
        )
    else:
        phrase = str(holder.get("companion_click_phrase") or "uh")
        result = companion_audio.submit(
            kind="interject",
            meeting_url=meeting_url,
            source="companion-interjector",
            metadata={"decision": decision, "phrase": phrase},
        )
        if result.get("accepted"):
            mark_companion_interjection_queued(holder, decision, now=at)
            holder["companion_click_last_suppression_key"] = None
            holder["companion_click_eligibility"] = "queued"
            return result
        reason = str(result.get("reason") or "queue-rejected")

    suppression_key = f"{reason}:{decision.get('eventKey') or decision.get('trigger')}"
    if holder.get("companion_click_last_suppression_key") != suppression_key:
        holder["companion_click_suppressed"] = int(holder.get("companion_click_suppressed") or 0) + 1
        holder["companion_click_last_suppression_key"] = suppression_key
    holder["companion_click_eligibility"] = reason
    return {"accepted": False, "reason": reason}


def active_caption_row_key(payload: dict[str, Any]) -> str | None:
    live_keys = payload.get("liveKeys") or []
    if live_keys:
        return str(live_keys[-1])
    rows = payload.get("rows") or []
    if rows and isinstance(rows[-1], dict):
        return str(rows[-1].get("key") or "") or None
    return None


def mark_companion_click_row_breaks(
    holder: dict[str, Any],
    status: dict[str, Any],
    payload: dict[str, Any],
    *,
    now: float | None = None,
) -> None:
    active_key = active_caption_row_key(payload)
    if not active_key:
        return
    at = time.time() if now is None else now
    pending = []
    for event in list(holder.get("companion_click_pending_breaks") or []):
        if not isinstance(event, dict):
            continue
        event_at = float(event.get("at") or 0.0)
        prior = event.get("priorHostKey")
        if not event.get("observed") and prior and active_key != prior and at - event_at <= 2.0:
            event["observed"] = True
            holder["companion_click_row_breaks_observed"] = int(holder.get("companion_click_row_breaks_observed") or 0) + 1
        if at - event_at <= 5.0:
            pending.append(event)
    holder["companion_click_pending_breaks"] = pending
    update_companion_click_status(status, holder)


def update_host_active_caption_state(holder: dict[str, Any], payload: dict[str, Any], *, now: float | None = None) -> None:
    active_key = active_caption_row_key(payload)
    rows = payload.get("rows") or []
    active_text = ""
    if active_key:
        for row in rows:
            if isinstance(row, dict) and str(row.get("key") or "") == active_key:
                active_text = str(row.get("text") or "")
                break
    at = time.monotonic() if now is None else now
    previous_key = holder.get("host_active_caption_key")
    previous_text = str(holder.get("host_active_caption_text") or "")
    if not active_key:
        holder["host_active_caption_key"] = None
        holder["host_active_caption_text"] = ""
        holder["host_active_caption_started_at"] = None
        holder["host_active_caption_last_growth_at"] = None
        return
    if active_key != previous_key:
        holder["host_active_caption_key"] = active_key
        holder["host_active_caption_text"] = active_text
        holder["host_active_caption_started_at"] = at
        holder["host_active_caption_last_growth_at"] = at
        return
    if len(active_text) > len(previous_text) or active_text != previous_text:
        holder["host_active_caption_text"] = active_text
        holder["host_active_caption_last_growth_at"] = at


def caption_key_parts(key: str | None) -> tuple[str | None, str]:
    text = str(key or "")
    role, sep, local_key = text.partition(":")
    if sep and role in CAPTION_ROLES:
        return role, local_key
    return None, text


def role_caption_key(role: str, key: str | None) -> str | None:
    if key is None:
        return None
    _existing_role, local_key = caption_key_parts(key)
    return f"{role}:{local_key}"


def normalize_caption_dedupe_pair(speaker: str, text: str) -> tuple[str, str]:
    return (
        _CAPTION_SPACE_RE.sub(" ", str(speaker or "").strip()).casefold(),
        _CAPTION_SPACE_RE.sub(" ", str(text or "").strip()).casefold(),
    )


class RecentCaptionDeduplicator:
    """Bounded cross-role duplicate detector for finalized Meet captions."""

    def __init__(
        self,
        *,
        window_seconds: float = CAPTION_DUPLICATE_WINDOW_SECONDS,
        limit: int = CAPTION_DUPLICATE_RECENT_LIMIT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.window_seconds = max(0.0, window_seconds)
        self.limit = max(1, limit)
        self._clock = clock
        self._recent: list[tuple[float, tuple[str, str], str, str]] = []

    def check_and_remember(self, *, role: str, key: str, speaker: str, text: str, final: bool) -> str | None:
        if not final:
            return None
        now = self._clock()
        cutoff = now - self.window_seconds
        self._recent = [entry for entry in self._recent if entry[0] >= cutoff]
        normalized = normalize_caption_dedupe_pair(speaker, text)
        duplicate_of = next(
            (
                previous_key
                for _at, previous_pair, previous_key, previous_role in self._recent
                if previous_pair == normalized and previous_role != role
            ),
            None,
        )
        if duplicate_of is None:
            self._recent.append((now, normalized, key, role))
            del self._recent[:-self.limit]
        return duplicate_of


def read_caption_payload(tab: Any, *, captions_js: str = CAPTIONS_JS) -> dict[str, Any]:
    raw = tab.evaluate(captions_js)
    return json.loads(raw) if isinstance(raw, str) else {"ok": False, "note": "no payload"}


def read_caption_payloads(
    holder: dict[str, Any],
    *,
    captions_js: str = CAPTIONS_JS,
    log: Callable[..., None] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    payloads = [("host", read_caption_payload(holder["tab"], captions_js=captions_js))]
    companion_tab = holder.get("companion_tab")
    if companion_tab is not None:
        try:
            payloads.append(("companion", read_caption_payload(companion_tab, captions_js=captions_js)))
        except Exception as error:  # noqa: BLE001
            if log is not None:
                log(f"[captions] companion read failed: {error}", err=True, role="companion")
    return payloads


def record_caption_raw_diagnostics(
    holder: dict[str, Any],
    status: dict[str, Any],
    payload: dict[str, Any],
    *,
    role: str = "host",
    now: float | None = None,
    history_limit: int = 50,
) -> None:
    """Keep raw Meet caption DOM text for diagnostics only."""

    role = role if role in CAPTION_ROLES else "host"
    if not any(key in payload for key in ("rawText", "rawRows", "rowCount", "childCount")):
        return

    at = time.time() if now is None else now
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at))
    raw_text = str(payload.get("rawText") or "")
    raw_rows = payload.get("rawRows")
    if not isinstance(raw_rows, list):
        raw_rows = []
    row_count = payload.get("rowCount")
    child_count = payload.get("childCount")

    snapshot = {
        "at": at,
        "iso": iso,
        "role": role,
        "rawText": raw_text,
        "rawRows": raw_rows,
        "rowCount": row_count,
        "childCount": child_count,
    }
    holder_raw_by_role = _ensure_raw_by_role(holder)
    status_raw_by_role = _ensure_raw_by_role(status)
    holder_role = holder_raw_by_role[role]
    status_role = status_raw_by_role[role]

    history = holder_role.setdefault("rawHistory", [])
    if not isinstance(history, list):
        history = []
        holder_role["rawHistory"] = history
    if (
        not history
        or history[-1].get("rawText") != raw_text
        or history[-1].get("rawRows") != raw_rows
        or history[-1].get("rowCount") != row_count
        or history[-1].get("childCount") != child_count
    ):
        history.append(snapshot)
        del history[:-history_limit]

    latest = {
        "rawText": raw_text,
        "rawRows": raw_rows,
        "rawAt": at,
        "rawIso": iso,
        "rawRowCount": row_count,
        "rawChildCount": child_count,
        "rawHistory": history,
        "rawHistoryCount": len(history),
    }
    holder_role.update(latest)
    status_role.update({**latest, "rawHistory": list(history)})
    if role == "host":
        holder.update(latest)
        holder["rawHistory"] = history
        status.update({**latest, "rawHistory": list(history)})
        status["rawHistoryCount"] = len(history)
    status["rawByRole"] = status_raw_by_role


def caption_raw_text_for_key(holder: dict[str, Any], key: str, *, role: str | None = None) -> str:
    parsed_role, local_key = caption_key_parts(key)
    raw_by_role = holder.get("rawByRole") or {}
    lookup_role = role or parsed_role or ("host" if isinstance(raw_by_role, dict) and "host" in raw_by_role else None)
    dom_key = local_key.split("#", 1)[0]
    raw_rows: list[Any] = []
    if lookup_role:
        role_state = raw_by_role.get(lookup_role)
        if isinstance(role_state, dict):
            raw_rows = list(role_state.get("rawRows") or [])
    if not raw_rows:
        raw_rows = list(holder.get("rawRows") or [])
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict):
            continue
        raw_key = str(raw_row.get("key") or "")
        if raw_key in {local_key, dom_key}:
            return str(raw_row.get("rawText") or "")
    return ""


def install_caption_push(tab: Any, *, role: str, log: Callable[..., None] | None = None) -> bool:
    first_attempt = role not in _CAPTION_PUSH_INSTALL_ATTEMPT_LOGGED
    _CAPTION_PUSH_INSTALL_ATTEMPT_LOGGED.add(role)
    lifecycle_interval = 0.0 if first_attempt else 60.0
    error_interval = 0.0 if first_attempt else 5.0
    try:
        _log_caption_push_lifecycle(
            log,
            role,
            "install-attempt",
            "[captions] push observer install attempt",
            interval=lifecycle_interval,
        )
        tab.call("Runtime.enable", timeout=5)
        try:
            tab.call("Runtime.addBinding", {"name": CAPTION_PUSH_BINDING}, timeout=5)
        except Exception as error:  # noqa: BLE001
            text = str(error).lower()
            if "already" not in text and "duplicate" not in text:
                raise
        _log_caption_push_lifecycle(
            log,
            role,
            "binding",
            f"[captions] binding registered: {CAPTION_PUSH_BINDING}",
            interval=lifecycle_interval,
        )
        tab.call("Page.enable", timeout=5)
        if not getattr(tab, "_ws_caption_new_doc_script_added", False):
            tab.call("Page.addScriptToEvaluateOnNewDocument", {"source": CAPTION_OBSERVER_JS}, timeout=5)
            try:
                setattr(tab, "_ws_caption_new_doc_script_added", True)
            except Exception:
                pass
            _log_caption_push_lifecycle(
                log,
                role,
                "new-document-script",
                "[captions] observer registered for new documents",
                interval=lifecycle_interval,
            )
        verdict = tab.evaluate(CAPTION_OBSERVER_JS, timeout=5)
        if not verdict:
            raise RuntimeError("caption observer returned no verdict")
        _log_caption_push_lifecycle(
            log,
            role,
            "observer",
            f"[captions] observer installed: {verdict}",
            interval=lifecycle_interval,
        )
        return True
    except Exception as error:  # noqa: BLE001
        _log_caption_push_error(
            log,
            role,
            "install",
            f"[captions] push observer unavailable ({error}); polling remains active",
            interval=error_interval,
        )
        return False


def apply_caption_payload(
    role: str,
    payload: dict[str, Any],
    *,
    holder: dict[str, Any],
    status: dict[str, Any],
    tracker: CaptionTracker,
    caption_emitter: Any,
    captions_lock: Any,
    fallback_logged_keys: set[str] | None = None,
    log: Callable[..., None] | None = None,
    transport: str = "poll",
    now: float | None = None,
) -> str:
    role = role if role in CAPTION_ROLES else "host"
    if transport == "push":
        with captions_lock:
            _mark_caption_push(status, role, now=now, log=log)
    if not payload.get("ok"):
        return str(payload.get("note") or "captions not found")
    payload = tracker.stabilize_payload_keys(payload)
    if role == "host":
        mark_companion_click_row_breaks(holder, status, payload, now=now)
        update_host_active_caption_state(holder, payload)
    click_artifact = role == "companion" and (time.time() if now is None else now) <= float(holder.get("companion_click_artifact_until") or 0.0)
    if click_artifact:
        payload = dict(payload)
        payload["rows"] = [
            ({**row, "clickArtifact": True} if isinstance(row, dict) else row)
            for row in (payload.get("rows") or [])
        ]
        payload["rawRows"] = [
            ({**row, "clickArtifact": True} if isinstance(row, dict) else row)
            for row in (payload.get("rawRows") or [])
        ]
    with captions_lock:
        record_caption_raw_diagnostics(holder, status, payload, role=role, now=now)
    if click_artifact:
        holder["companion_click_artifacts_suppressed"] = int(holder.get("companion_click_artifacts_suppressed") or 0) + len(payload.get("rows") or [])
        status["companionClickArtifactsSuppressed"] = holder["companion_click_artifacts_suppressed"]
        return str(payload.get("note") or "")
    if fallback_logged_keys is not None:
        for row in payload.get("rows") or []:
            fallback_key = role_caption_key(role, row.get("key"))
            if row.get("speaker") == "Speaker" and fallback_key not in fallback_logged_keys:
                fallback_logged_keys.add(fallback_key or "")
                if log is not None:
                    log(f"[captions] speaker-split fallback used: {row.get('text', '')[:100]!r}", role=role)
    tracker.update(
        payload.get("rows") or [],
        payload.get("liveKeys") or [],
        lambda key, speaker, text, final=False, replaces=None: caption_emitter.emit(
            role,
            key,
            speaker,
            text,
            final=final,
            replaces=replaces,
        ),
    )
    return str(payload.get("note") or "")


def drain_caption_push_events(
    role: str,
    tab: Any,
    *,
    holder: dict[str, Any],
    status: dict[str, Any],
    tracker: CaptionTracker,
    caption_emitter: Any,
    captions_lock: Any,
    fallback_logged_keys: set[str] | None = None,
    log: Callable[..., None] | None = None,
) -> int:
    drain = getattr(tab, "drain_events", None)
    if drain is None:
        return 0
    handled = 0
    for event in drain():
        try:
            if not isinstance(event, dict) or event.get("method") != "Runtime.bindingCalled":
                continue
            params = event.get("params") or {}
            if not isinstance(params, dict) or params.get("name") != CAPTION_PUSH_BINDING:
                continue
            payload_text = params.get("payload") or "{}"
            if not isinstance(payload_text, str):
                _log_caption_push_error(log, role, "type", "[captions] invalid push payload type; skipped")
                continue
            if len(payload_text.encode("utf-8", errors="ignore")) > CAPTION_PUSH_MAX_PAYLOAD_BYTES:
                _log_caption_push_error(log, role, "oversized", "[captions] oversized push payload skipped")
                continue
            try:
                payload = json.loads(payload_text)
            except (TypeError, ValueError) as error:
                _log_caption_push_error(log, role, "json", f"[captions] invalid push payload skipped: {error}")
                continue
            if not isinstance(payload, dict):
                _log_caption_push_error(log, role, "shape", "[captions] non-object push payload skipped")
                continue
            if role not in _CAPTION_PUSH_FIRST_FRAME_LOGGED:
                _CAPTION_PUSH_FIRST_FRAME_LOGGED.add(role)
                _log_caption_push_lifecycle(log, role, "first-frame", "[captions] first push frame received", interval=0.0)
            apply_caption_payload(
                role,
                payload,
                holder=holder,
                status=status,
                tracker=tracker,
                caption_emitter=caption_emitter,
                captions_lock=captions_lock,
                fallback_logged_keys=fallback_logged_keys,
                log=log,
                transport="push",
            )
            handled += 1
        except Exception as error:  # noqa: BLE001 - malformed push data must never stop polling.
            _log_caption_push_error(log, role, "consume", f"[captions] push payload skipped: {error}")
            continue
    return handled


class CaptionEmitter:
    def __init__(
        self,
        *,
        holder: dict[str, Any],
        status: dict[str, Any],
        captions_log: list[dict[str, Any]],
        captions_index: dict[str, int],
        captions_lock: Any,
        mailbox: Any,
        recipients: list[str],
        ignore: set[str],
        self_name: str,
        sender_prefix: str,
        deduplicator: RecentCaptionDeduplicator | None = None,
        printer: Callable[[str], None] = print,
    ) -> None:
        self.holder = holder
        self.status = status
        self.captions_log = captions_log
        self.captions_index = captions_index
        self.captions_lock = captions_lock
        self.mailbox = mailbox
        self.recipients = recipients
        self.ignore = ignore
        self.self_name = self_name
        self.sender_prefix = sender_prefix
        self.deduplicator = deduplicator or RecentCaptionDeduplicator()
        self.printer = printer

    def emit(self, role: str, key: str, speaker: str, text: str, final: bool = False, replaces: str | None = None) -> None:
        role = role if role in CAPTION_ROLES else "host"
        if speaker.strip().lower() in self.ignore:
            return
        if speaker.strip().lower() in ("you", "sie", "tu", "vous"):
            speaker = self.self_name
        key = role_caption_key(role, key) or f"{role}:"
        replaces = role_caption_key(role, replaces)
        sender = self.sender_prefix + (re.sub(r"[^a-z0-9]+", "-", speaker.lower()).strip("-") or "speaker")
        line = f"{speaker}: {text}"
        meeting_url_now = self.holder.get("url")
        duplicate_of = self.deduplicator.check_and_remember(
            role=role,
            key=key,
            speaker=speaker,
            text=text,
            final=final,
        )
        full_meta = {
            "source": "google-meet-captions", "speaker": speaker, "key": key,
            "final": final, "replaces": replaces, "meetingUrl": meeting_url_now,
            "role": role, "duplicateOf": duplicate_of,
        }
        if duplicate_of is None:
            if final:
                try:
                    self.mailbox.ingest_transcript(
                        text,
                        correlation_id=f"meet-caption:{room_id(meeting_url_now) or 'unknown'}:{key}",
                        source_kind="operator" if speaker == self.self_name else "unknown",
                        audio_meta=dict(full_meta),
                    )
                except Exception as error:  # noqa: BLE001
                    print(f"[stt] google_meet ingest failed: {error}", file=sys.stderr, flush=True)
            for recipient in self.recipients:
                try:
                    self.mailbox.send(recipient, line, sender=sender, metadata=dict(full_meta))
                except Exception as error:  # noqa: BLE001
                    print(f"[mailbox] send failed: {error}", file=sys.stderr, flush=True)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self.captions_lock:
            raw_text = caption_raw_text_for_key(self.holder, key, role=role)
            now = time.time()
            idx = self.captions_index.get(key)
            row_payload = {
                "text": text, "updated_at": now, "iso": now_iso,
                "final": final, "replaces": replaces, "meetingUrl": meeting_url_now,
                "rawText": raw_text, "role": role, "duplicateOf": duplicate_of,
            }
            if idx is not None and idx < len(self.captions_log) and self.captions_log[idx].get("key") == key:
                row = self.captions_log[idx]
                previous_raw_text = row.get("rawText", "")
                row.update(row_payload)
                if not raw_text:
                    row["rawText"] = previous_raw_text
            else:
                self.captions_log.append({
                    "key": key, "at": now, "updated_at": now, "iso": now_iso,
                    "speaker": speaker, "text": text, "meetingUrl": meeting_url_now,
                    "final": final, "replaces": replaces, "rawText": raw_text,
                    "role": role, "duplicateOf": duplicate_of,
                })
                del self.captions_log[:-200]
                self.captions_index.clear()
                for i, row in enumerate(self.captions_log):
                    self.captions_index[row["key"]] = i
            self.status["captionCount"] = len(self.captions_log)
        self.status["emitCount"] = int(self.status.get("emitCount") or 0) + 1
        self.status["lastCaptionAt"] = now_iso
        transport = (
            (self.status.get("captionTransportByRole") or {}).get(role, {}).get("captionTransport")
            or self.status.get("captionTransport")
            or "poll"
        )
        detail = {
            "role": role,
            "key": key,
            "replaces": replaces,
            "rawText": raw_text,
            "captionTransport": transport,
        }
        if duplicate_of is None:
            self.printer(f"[caption] {line} {json.dumps(detail, ensure_ascii=False, separators=(',', ':'))}")
        else:
            detail["duplicateOf"] = duplicate_of
            self.printer(
                f"[caption:{role}:duplicate] {line} (duplicate of {duplicate_of}) "
                f"{json.dumps(detail, ensure_ascii=False, separators=(',', ':'))}"
            )


def parse_role_authusers(values: list[str] | None) -> dict[str, int]:
    resolved: dict[str, int] = {}
    for raw in values or []:
        text = str(raw or "").strip()
        if "=" not in text:
            raise SystemExit(f"--role-authuser expects ROLE=N, got: {text!r}")
        role, authuser_text = text.split("=", 1)
        role_name = role.strip().lower()
        if not role_name:
            raise SystemExit(f"--role-authuser expects ROLE=N, got: {text!r}")
        try:
            authuser = int(authuser_text)
        except ValueError as error:
            raise SystemExit(f"--role-authuser expects a non-negative integer authuser, got: {text!r}") from error
        if authuser < 0:
            raise SystemExit(f"--role-authuser expects a non-negative integer authuser, got: {text!r}")
        resolved[role_name] = authuser
    return resolved


def parse_role_emails(values: list[str] | None) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for raw in values or []:
        text = str(raw or "").strip()
        if "=" not in text:
            raise SystemExit(f"--role-email expects ROLE=EMAIL, got: {text!r}")
        role, email = text.split("=", 1)
        role_name = role.strip().lower()
        email_address = email.strip().lower()
        if not role_name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email_address):
            raise SystemExit(f"--role-email expects ROLE=EMAIL, got: {text!r}")
        resolved[role_name] = email_address
    return resolved


def authuser_from_url(url: str | None) -> int | None:
    try:
        params = dict(parse_qsl(urlsplit(str(url or "")).query, keep_blank_values=True))
    except Exception:
        return None
    try:
        value = params.get("authuser")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def with_authuser(url: str, authuser: int | None) -> str:
    if authuser is None:
        return url
    parts = urlsplit(url)
    params = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "authuser"]
    params.append(("authuser", str(authuser)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def find_controlled_meet_tab(
    cdp_endpoint: str,
    expected_authuser: int | None,
    *,
    require_room: bool = False,
    wanted_room: str | None = None,
    exclude_id: str | None = None,
    exclude_room: str | None = None,
) -> dict[str, Any] | None:
    """Find the Meet connector tab owned by one dynamically assigned SSO slot."""
    target_room = room_id(wanted_room)
    skipped_room = room_id(exclude_room)
    for tab in list_tabs(cdp_endpoint):
        url = str(tab.get("url") or "")
        if tab.get("type") != "page" or "meet.google.com" not in url:
            continue
        if exclude_id and str(tab.get("id") or "") == str(exclude_id):
            continue
        current_room = room_id(url)
        if require_room and current_room is None:
            continue
        if target_room and current_room != target_room:
            continue
        if skipped_room and current_room == skipped_room:
            continue
        if expected_authuser is not None and authuser_from_url(url) != expected_authuser:
            continue
        return tab
    return None


def sso_preflight_ready(
    accounts: list[dict[str, Any]],
    *,
    required_authusers: set[int],
    required_accounts: dict[int, str] | None = None,
    minimum_accounts: int = 2,
) -> bool:
    signed_in = {
        int(account["authuser"]): str(account.get("email") or "").strip().lower()
        for account in accounts
        if account.get("signedIn") is True
        and str(account.get("email") or "").strip()
        and isinstance(account.get("authuser"), int)
    }
    if len(set(signed_in.values())) < minimum_accounts or not required_authusers.issubset(signed_in):
        return False
    return all(signed_in.get(authuser) == email.strip().lower() for authuser, email in (required_accounts or {}).items())


def sso_probe_authusers(role_authusers: dict[str, int] | None) -> list[int]:
    slots = {
        int(authuser)
        for authuser in (role_authusers or {}).values()
        if isinstance(authuser, int) and authuser >= 0
    }
    return sorted(slots) if slots else list(DEFAULT_SSO_AUTHUSER_PROBE_SLOTS)


def cached_sso_accounts_status(holder: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    cached = holder.get("sso_accounts")
    accounts = list(cached) if isinstance(cached, list) else []
    scanned_at_raw = holder.get("sso_accounts_scanned_at")
    satisfied_at_raw = holder.get("sso_satisfied_at")
    try:
        scanned_at = float(scanned_at_raw) if scanned_at_raw is not None else None
    except (TypeError, ValueError):
        scanned_at = None
    try:
        satisfied_at = float(satisfied_at_raw) if satisfied_at_raw is not None else None
    except (TypeError, ValueError):
        satisfied_at = None
    satisfied = bool(holder.get("sso_satisfied"))
    stale = False if satisfied else cached is None or scanned_at is None or ((time.time() if now is None else now) - scanned_at) >= 10.0
    return {
        "ssoAccounts": accounts,
        "ssoAccountsScannedAt": scanned_at,
        "ssoAccountsStale": stale,
        "ssoSatisfied": satisfied,
        "ssoSatisfiedAt": satisfied_at,
    }


def sso_resolved_account_map(
    accounts: list[dict[str, Any]],
    *,
    role_authusers: dict[str, int],
    role_emails: dict[str, str],
    required_roles: list[str] | tuple[str, ...],
    verified_roles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]] | None:
    resolved: dict[str, dict[str, Any]] = {}
    verified = verified_roles or {}
    for role in required_roles:
        expected_authuser = role_authusers.get(role)
        expected_email = str(role_emails.get(role) or "").strip().lower()
        verified_email = str((verified.get(role) or {}).get("email") or "").strip().lower()
        if expected_authuser is None or not expected_email or verified_email != expected_email:
            return None
        matched = next(
            (
                account
                for account in accounts
                if account.get("signedIn") is True
                and account.get("authuser") == expected_authuser
                and str(account.get("email") or "").strip().lower() == expected_email
            ),
            None,
        )
        if matched is None:
            return None
        resolved[role] = dict(matched)
    return resolved


def update_sso_satisfaction(
    holder: dict[str, Any],
    *,
    role_authusers: dict[str, int],
    role_emails: dict[str, str],
    required_roles: list[str] | tuple[str, ...],
    now: float | None = None,
) -> bool:
    resolved = sso_resolved_account_map(
        list(holder.get("sso_accounts") or []),
        role_authusers=role_authusers,
        role_emails=role_emails,
        required_roles=required_roles,
        verified_roles=holder.get("sso_verified_roles") or {},
    )
    if resolved is None:
        holder["sso_satisfied"] = False
        holder["sso_satisfied_at"] = None
        holder["sso_resolved_accounts"] = {}
        return False
    holder["sso_satisfied"] = True
    holder["sso_satisfied_at"] = time.time() if now is None else now
    holder["sso_resolved_accounts"] = resolved
    holder["sso_rescan_permitted"] = False
    return True


def invalidate_sso_satisfaction(
    holder: dict[str, Any],
    reason: str,
    *,
    now: float | None = None,
    clear_verified: bool = True,
    clear_roles: list[str] | tuple[str, ...] | None = None,
) -> None:
    holder["sso_satisfied"] = False
    holder["sso_satisfied_at"] = None
    holder["sso_resolved_accounts"] = {}
    if clear_roles is not None:
        verified = dict(holder.get("sso_verified_roles") or {})
        for role in clear_roles:
            verified.pop(role, None)
        holder["sso_verified_roles"] = verified
    elif clear_verified:
        holder["sso_verified_roles"] = {}
    holder["sso_rescan_permitted"] = True
    holder["sso_invalidated_reason"] = reason
    holder["sso_invalidated_at"] = time.time() if now is None else now


def scan_sso_accounts_if_permitted(
    holder: dict[str, Any],
    scanner: Callable[[], list[dict[str, Any]]],
    *,
    allow_scan: bool = False,
    role_authusers: dict[str, int],
    role_emails: dict[str, str],
    required_roles: list[str] | tuple[str, ...],
    now: float | None = None,
) -> list[dict[str, Any]]:
    if holder.get("sso_satisfied") and not allow_scan:
        return list(holder.get("sso_accounts") or [])
    if not allow_scan and not holder.get("sso_rescan_permitted"):
        return list(holder.get("sso_accounts") or [])
    holder["sso_rescan_permitted"] = False
    accounts = scanner()
    holder["sso_accounts"] = list(accounts)
    holder["sso_accounts_scanned_at"] = time.time() if now is None else now
    update_sso_satisfaction(
        holder,
        role_authusers=role_authusers,
        role_emails=role_emails,
        required_roles=required_roles,
        now=now,
    )
    return list(accounts)


def match_role_account(
    dom_account: dict[str, Any] | None,
    *,
    tab_url: str,
    expected_authuser: int | None,
    expected_email: str,
    scanned_accounts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Verify a role from Meet's DOM or its preflight-validated authuser slot."""
    wanted_email = expected_email.strip().lower()
    actual_email = str((dom_account or {}).get("email") or "").strip().lower()
    if actual_email:
        if actual_email != wanted_email:
            raise ValueError(f"browser page uses {actual_email}, expected {wanted_email}")
        if (dom_account or {}).get("signedIn") is True:
            return dict(dom_account or {})
    if expected_authuser is None or authuser_from_url(tab_url) != expected_authuser:
        return None
    for account in scanned_accounts:
        if (
            account.get("authuser") == expected_authuser
            and account.get("signedIn") is True
            and str(account.get("email") or "").strip().lower() == wanted_email
        ):
            return dict(account)
    return None


def wait_for_sso_preflight(
    cdp_endpoint: str,
    *,
    required_authusers: set[int],
    required_accounts: dict[int, str] | None = None,
    browser_process: subprocess.Popen[Any] | None,
) -> list[dict[str, Any]]:
    probe_slots = sorted({0, 1, *required_authusers})
    consent_operation_id = f"sso-preflight:{uuid.uuid4().hex}"
    last_summary: tuple[int, tuple[int, ...]] | None = None
    while True:
        try:
            accounts = scan_signed_in_sso_accounts(
                cdp_endpoint,
                authusers=probe_slots,
                reason="sso-preflight",
                detail=(
                    "Meet bridge startup is verifying the assigned Google authuser slots "
                    f"before joining; required slots={sorted(required_authusers)}"
                ),
                role="probe",
                component="meet_bridge",
                sso_satisfied=False,
                consent_operation_id=consent_operation_id,
            )
        except navigator.NavigationBlockedError as error:
            raise SystemExit(f"SSO preflight stopped before probing: {error}") from error
        signed_slots = tuple(sorted(int(account["authuser"]) for account in accounts))
        summary = (len({str(account.get("email") or "").lower() for account in accounts}), signed_slots)
        if summary != last_summary:
            print(
                f"[bridge] SSO preflight: {summary[0]}/2 distinct Google accounts signed in "
                f"(live authuser slots: {', '.join(map(str, signed_slots)) or 'none'})",
                flush=True,
            )
            last_summary = summary
        if sso_preflight_ready(
            accounts,
            required_authusers=required_authusers,
            required_accounts=required_accounts,
        ):
            print("[bridge] SSO preflight passed -- starting Meet drivers", flush=True)
            return accounts
        if browser_process is not None and browser_process.poll() is not None:
            raise SystemExit("Meet browser closed before two Google accounts were signed in.")
        time.sleep(2.0)


def _terminate_process(process: subprocess.Popen[Any] | None) -> bool:
    if process is None:
        return False
    if process.poll() is not None:
        return True
    process.terminate()
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if process.poll() is not None:
            return True
        time.sleep(0.1)
    process.kill()
    return process.poll() is not None


def message_text(message: dict[str, Any]) -> str:
    for key in ("text", "message", "body", "content"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def main() -> None:
    migrated_default = ensure_default_profile_migrated()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cdp", default=os.environ.get("MEET_BRIDGE_CDP", DEFAULT_CDP), help="Chrome DevTools endpoint for attach mode (default %(default)s)")
    parser.add_argument("--meet", default=None, help="Google Meet URL -- pops up the bridge's own browser for SSO sign-in and joins there")
    parser.add_argument("--new", action="store_true", help="CREATE a new instant meeting (meet.google.com/new) for the signed-in account, join it, and post its link to the mailbox")
    parser.add_argument("--attach-only", action="store_true", help="Never pop a browser: only attach to an existing meet tab on --cdp")
    parser.add_argument("--companion", action="store_true", help="ALSO keep a second signed-in account in the Chrome profile sitting MUTED in the meeting so Google sees 2 participants and won't end/nag the servant meeting.")
    parser.add_argument("--companion-listen-device", default="", help="Deprecated compatibility option; companion playback remains muted. Use --companion-heard-stt for direct remote-MediaStream capture.")
    parser.add_argument("--companion-heard-stt", action="store_true", help="Opt in to tapping the muted companion's remote MediaStream into server-side Whisper and other non-Meet STT engines.")
    parser.add_argument("--companion-heard-stt-input-device", default=os.environ.get("WS_COLLAB_COMPANION_HEARD_STT_INPUT_DEVICE", ""), help="Deprecated compatibility option; direct browser MediaStream capture does not require a virtual audio device.")
    parser.add_argument("--companion-click", action="store_true", help="Opt in to companion synthetic-mic boundary sounds; keeps the companion unmuted while enabled.")
    parser.add_argument("--companion-click-mode", choices=["reactive", "fixed"], default="reactive", help="Companion click scheduling: reactive breaks long host caption rows; fixed clicks every --companion-click-interval seconds (default %(default)s)")
    parser.add_argument("--companion-click-trigger", choices=["caption", "audio", "both"], default="caption", help="Reactive pause detector: caption stasis, incoming audio RMS, or both (default %(default)s)")
    parser.add_argument("--companion-click-interval", type=companion_click_interval, default=2.0, help="Seconds between companion synthetic-mic clicks in fixed mode (default %(default)s)")
    parser.add_argument("--companion-click-after", type=companion_click_positive_float, default=10.0, help="Reactive mode threshold: click after the current host caption row has grown this many seconds (default %(default)s)")
    parser.add_argument("--companion-click-silence-ms", type=companion_click_ms, default=500.0, help="Reactive pause threshold: active host row or audio must be quiet this many milliseconds (default %(default)s)")
    parser.add_argument("--companion-click-min-gap", type=companion_click_positive_float, default=6.0, help="Minimum seconds between reactive companion clicks (default %(default)s)")
    parser.add_argument("--companion-click-max-wait", type=companion_click_nonnegative_float, default=0.0, help="Optional hard ceiling in seconds before forcing a boundary sound despite no pause; 0 disables (default %(default)s)")
    parser.add_argument("--companion-click-audio-rms-threshold", type=companion_click_nonnegative_float, default=0.015, help="Incoming audio RMS threshold for --companion-click-trigger=audio|both (default %(default)s)")
    parser.add_argument("--companion-click-ms", type=companion_click_ms, default=100.0, help="Companion click burst duration in milliseconds (default %(default)s)")
    parser.add_argument("--companion-click-gain", type=companion_click_gain, default=0.12, help="Companion click WebAudio gain in (0,1]; tune empirically for Meet VAD (default %(default)s)")
    parser.add_argument("--companion-click-sound", choices=["uh", "click"], default="uh", help="Legacy companion boundary sound compatibility option (default %(default)s)")
    parser.add_argument("--companion-click-phrase", choices=["uh", "uhuh", "hmm"], default=None, help="Companion backchannel phrase; defaults to the legacy --companion-click-sound value")
    parser.add_argument("--companion-click-f0", type=companion_click_positive_float, default=125.0, help="'uh' fundamental frequency in Hz (default %(default)s)")
    parser.add_argument("--companion-click-f1", type=companion_click_positive_float, default=600.0, help="'uh' first formant bandpass center in Hz (default %(default)s)")
    parser.add_argument("--companion-click-f2", type=companion_click_positive_float, default=1300.0, help="'uh' second formant bandpass center in Hz (default %(default)s)")
    parser.add_argument("--status-port", type=int, default=48699, help="Local health/status HTTP port -- what ws_collab's google_meet STT driver and admin UI read (0 disables; default %(default)s)")
    parser.add_argument("--self-name", default="You", help="Name captions attribute to the bridge account's own mic (Meet shows 'You'; default %(default)s)")
    parser.add_argument("--no-autojoin", action="store_true", help="Do not auto-click Join/mic/captions -- drive the Meet window manually")
    parser.add_argument("--browser", default=None, help="Path to chrome.exe/msedge.exe for the popup (auto-detected)")
    parser.add_argument("--browser-backend", choices=["windows", "wsl"], default=os.environ.get("MEET_BRIDGE_BROWSER_BACKEND", "windows"), help="How to host the Chrome window(s): 'windows' (default, a normal visible window) or 'wsl' (runs inside WSL2 under a real Xvfb virtual display -- genuinely invisible on the Windows desktop, not just off-screen)")
    parser.add_argument("--wsl-distro", default=os.environ.get("MEET_BRIDGE_WSL_DISTRO"), help="WSL distro name for --browser-backend wsl (default: first distro from `wsl -l -q`)")
    parser.add_argument("--profile", default=str(migrated_default), help="Persistent profile dir for the popup browser (keeps your SSO login; default %(default)s)")
    parser.add_argument("--port", type=int, default=DEFAULT_POPUP_PORT, help="DevTools port for the popup browser (default %(default)s)")
    parser.add_argument("--role-authuser", action="append", default=None, help="Map a role to an authuser slot, e.g. --role-authuser host=0 --role-authuser companion=1 (future guest/client slots can be added the same way)")
    parser.add_argument("--role-email", action="append", default=None, help="Expected signed-in email for a role, e.g. --role-email host=person@example.com; startup refuses a mismatched authuser slot")
    parser.add_argument("--forget-sso", action="store_true", help="Wipe the popup browser's stored Google login (profile dir) and exit -- use when the SSO session expired or to switch accounts")
    parser.add_argument("--list-tabs", action="store_true", help="List CDP tabs and exit")
    parser.add_argument("--mailbox-base", default=os.environ.get("WS_COLLAB_MAILBOX_BASE", DEFAULT_MAILBOX_BASE), help="ws_collab REST base URL for mailbox IN/OUT (default %(default)s)")
    parser.add_argument("--to", action="append", default=None, help="ws_collab mailbox/mailboxes for finished captions (default conversation)")
    parser.add_argument("--sender-prefix", default=DEFAULT_SENDER_PREFIX, help="Caption sender prefix (default %(default)s + speaker)")
    parser.add_argument("--outbox", default=DEFAULT_OUTBOX, help="ws_collab mailbox the bridge WATCHES for outgoing lines/commands (default %(default)s)")
    parser.add_argument("--no-out", action="store_true", help="Disable the mailbox -> Meet chat direction")
    parser.add_argument("--speak", action="store_true", help="Also speak outgoing lines with Windows TTS")
    parser.add_argument("--settle", type=float, default=1.2, help="Unused, kept for CLI compatibility (default %(default)s)")
    parser.add_argument("--poll", type=float, default=0.4, help="Caption poll interval seconds (default %(default)s)")
    parser.add_argument("--ignore-speaker", action="append", default=[], help="Speaker name(s) to skip (repeatable)")
    parser.add_argument("--list-audio-devices", action="store_true", help="List Windows audio devices (index, name, in/out channels) and exit -- use this to find a virtual cable's exact name")
    parser.add_argument("--tts-output-device", default=os.environ.get("MEET_BRIDGE_TTS_OUTPUT_DEVICE"), help="Name (substring) of a real playback device to route /say speech to, e.g. a virtual cable's 'Input' side -- omit to keep using the in-page WebAudio synthetic mic (env MEET_BRIDGE_TTS_OUTPUT_DEVICE)")
    parser.add_argument("--companion-audio-queue-max", type=int, default=int(os.environ.get("WS_COLLAB_COMPANION_AUDIO_QUEUE_MAX", "8")), help="Maximum pending companion speech/interject audio items (default %(default)s)")
    parser.add_argument("--mic-select-device", default=os.environ.get("MEET_BRIDGE_MIC_SELECT_DEVICE"), help="Name (substring) of the device Meet's own Audio Settings mic dropdown should select, e.g. a virtual cable's 'Output' side -- omit to leave Meet's mic selection alone (env MEET_BRIDGE_MIC_SELECT_DEVICE)")
    args = parser.parse_args()
    args.role_authusers = parse_role_authusers(args.role_authuser)
    args.role_emails = parse_role_emails(args.role_email)
    mailbox = MailboxClient(args.mailbox_base)
    nav_poster = BrowserNavIntentPoster(mailbox)
    nav_instance_port = urlsplit(args.cdp).port if args.attach_only else args.port
    settings_dir = Path(os.environ.get("WS_COLLAB_STATE_DIR") or DEFAULT_PROFILE.parent).expanduser()
    navigator.set_consent_required_provider(
        lambda: read_sso_consent_setting(settings_dir)
    )
    configure_browser_nav_logging(
        nav_poster.submit,
        instance=f"meet-bridge:{os.getpid()}:{nav_instance_port or args.port}",
        component="meet_bridge",
        role="host",
        cdp_endpoint=args.cdp if args.attach_only else f"http://127.0.0.1:{args.port}",
        chrome_profile=Path(args.profile).expanduser(),
    )
    if args.companion_click and not args.companion:
        raise SystemExit("--companion-click requires --companion")
    if args.companion_heard_stt and not args.companion:
        raise SystemExit("--companion-heard-stt requires --companion")

    def role_authuser(role: str) -> int | None:
        wanted = str(role or "").strip().lower()
        return args.role_authusers.get(wanted)

    def role_target_url(url: str, role: str) -> str:
        return with_authuser(url, role_authuser(role))

    def role_email(role: str) -> str | None:
        return args.role_emails.get(str(role or "").strip().lower())

    def verify_role_tab_account(tab: CdpTab, role: str, timeout: float = 10.0) -> dict[str, Any]:
        expected_email = role_email(role)
        if not expected_email:
            raise RuntimeError(f"{role} has no expected SSO email")
        deadline = time.time() + timeout
        while time.time() < deadline:
            account = read_google_account(tab)
            try:
                tab_url = str(tab.evaluate("location.href") or "")
            except Exception:
                tab_url = ""
            try:
                verified = match_role_account(
                    account,
                    tab_url=tab_url,
                    expected_authuser=role_authuser(role),
                    expected_email=expected_email,
                    scanned_accounts=signed_sso_accounts,
                )
            except ValueError as error:
                raise RuntimeError(f"{role} {error}") from error
            if verified:
                return verified
            time.sleep(0.25)
        raise RuntimeError(f"could not verify {role} browser page as {expected_email}")

    def find_role_meet_tab(
        cdp: str,
        role: str = "host",
        *,
        require_room: bool = False,
        wanted_room: str | None = None,
        exclude_id: str | None = None,
        exclude_room: str | None = None,
    ) -> dict[str, Any] | None:
        return find_controlled_meet_tab(
            cdp,
            role_authuser(role),
            require_room=require_room,
            wanted_room=wanted_room,
            exclude_id=exclude_id,
            exclude_room=exclude_room,
        )

    def wait_for_role_meet_tab(
        cdp: str,
        role: str = "host",
        *,
        timeout: float = 900.0,
        require_room: bool = False,
        wanted_room: str | None = None,
        exclude_room: str | None = None,
    ) -> dict[str, Any]:
        told = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                tab = find_role_meet_tab(
                    cdp,
                    role,
                    require_room=require_room,
                    wanted_room=wanted_room,
                    exclude_room=exclude_room,
                )
            except Exception:
                tab = None
            if tab:
                return tab
            if not told:
                told = True
                print("[bridge] waiting for a meet.google.com tab -- sign in and open the meeting in the popped-up window...", flush=True)
            time.sleep(1.5)
        raise SystemExit("Timed out waiting for a Google Meet tab (15 min).")

    if args.list_audio_devices:
        list_audio_devices()
        return

    tts_output_device_index: int | None = None
    if args.tts_output_device:
        try:
            tts_output_device_index = resolve_audio_device(args.tts_output_device, want="output")
            print(f"[bridge] /say will play through device #{tts_output_device_index} matching {args.tts_output_device!r}", flush=True)
        except ValueError as error:
            raise SystemExit(f"--tts-output-device: {error}")

    if args.forget_sso:
        import shutil

        profile = Path(args.profile).expanduser()
        if cdp_alive(f"http://127.0.0.1:{args.port}"):
            raise SystemExit("Close the bridge browser window first, then rerun --forget-sso.")
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
            print(f"[bridge] SSO profile wiped: {profile} -- the next --meet run asks for the account again", flush=True)
        else:
            print(f"[bridge] nothing to forget ({profile} does not exist)", flush=True)
        return

    assigned_authusers: list[int] = []
    if not args.list_tabs:
        required_roles = ["host", *(["companion"] if args.companion else [])]
        missing_roles = [role for role in required_roles if role_authuser(role) is None]
        if missing_roles:
            raise SystemExit(
                "Assign signed-in SSO accounts on the Google Meet admin page before starting "
                f"the drivers (missing --role-authuser for: {', '.join(missing_roles)})."
            )
        missing_emails = [role for role in required_roles if role_email(role) is None]
        if missing_emails:
            raise SystemExit(
                "Assign signed-in SSO accounts on the Google Meet admin page before starting "
                f"the drivers (missing --role-email for: {', '.join(missing_emails)})."
            )
        assigned_authusers = [int(role_authuser(role)) for role in required_roles]
        if len(assigned_authusers) != len(set(assigned_authusers)):
            raise SystemExit("host and companion require distinct signed-in SSO accounts")

    # Browser setup is account-centric. Meet automation is not initialized
    # until two distinct Google sessions are live in this one profile.
    cdp_endpoint = args.cdp
    # A normal startup opens the requested Meet surface. AccountChooser is only
    # opened by an explicit setup/add-account action.
    args.launch_url = args.meet or "https://meet.google.com/new"
    if not args.attach_only:
        cdp_endpoint, host_process = launch_browser(args)
    else:
        host_process = None
        try:
            browser_profile_root(
                cdp_endpoint,
                reason="profile-probe",
                detail=f"attach-only bridge is resolving the live Chrome profile on {cdp_endpoint}",
                role="probe",
                component="meet_bridge",
            )
        except Exception:
            pass

    if args.list_tabs:
        for tab_entry in list_tabs(cdp_endpoint):
            print(f"{tab_entry.get('type'):8} {tab_entry.get('title', '')[:60]!r} {tab_entry.get('url', '')[:90]}", flush=True)
        return

    required_authusers = set(assigned_authusers)
    required_accounts = {
        int(role_authuser(role)): str(role_email(role))
        for role in required_roles
    }
    signed_sso_accounts = wait_for_sso_preflight(
        cdp_endpoint,
        required_authusers=required_authusers,
        required_accounts=required_accounts,
        browser_process=host_process,
    )

    recipients = args.to or list(DEFAULT_RECIPIENTS)
    ignore = {name.strip().lower() for name in args.ignore_speaker}

    created_servant = False
    if args.attach_only:
        tab_info = find_role_meet_tab(cdp_endpoint, "host")
        if not tab_info:
            raise SystemExit(
                f"No meet.google.com tab found via {cdp_endpoint}.\n"
                "Either rerun without --attach-only (pops up an SSO browser window), or\n"
                "start Chrome with --remote-debugging-port=9222, join the Meet, then rerun."
            )
        tab_info, _ = reuse_or_open_tab(
            cdp_endpoint,
            str(tab_info.get("url") or ""),
            existing_in_scope=tab_info,
            reason="reattach-lost-tab",
            detail="attach-only mode found an existing host Meet tab and is bringing it to the foreground",
            role="host",
            component="meet_bridge",
            intended_identity=role_email("host"),
        )
    else:
        wanted_url = args.meet or "https://meet.google.com/new"
        exact_tab = find_role_meet_tab(
            cdp_endpoint,
            "host",
            wanted_room=args.meet,
        )
        reusable_tab = exact_tab or find_role_meet_tab(cdp_endpoint, "host")
        previous_room = str((reusable_tab or {}).get("url") or "")
        navigate_existing = bool(args.new or (args.meet and exact_tab is None))
        if reusable_tab is None:
            print(f"[host] no existing browser page for {role_email('host')}; creating one with authuser={role_authuser('host')}", flush=True)
        else:
            print(f"[host] reusing browser page for {role_email('host')}: {reusable_tab.get('url')}", flush=True)
        tab_info, reused_tab = reuse_or_open_tab(
            cdp_endpoint,
            role_target_url(wanted_url, "host"),
            existing_in_scope=reusable_tab,
            navigate_existing=navigate_existing,
            reason="new-meeting" if args.new or not args.meet else "join-meeting",
            detail=(
                f"host connector for {role_email('host')} "
                + ("is creating a fresh Meet room" if args.new or not args.meet else f"is joining {args.meet}")
            ),
            role="host",
            component="meet_bridge",
            intended_identity=role_email("host"),
        )
        if tab_info is None:
            raise SystemExit("Could not open or reuse the host Meet connector tab.")
        if navigate_existing or not reused_tab:
            tab_info = wait_for_role_meet_tab(
                cdp_endpoint,
                "host",
                require_room=True,
                wanted_room=args.meet,
                exclude_room=previous_room if args.new and reused_tab else None,
            )
            created_servant = not args.meet

    host_tab = CdpTab(tab_info["webSocketDebuggerUrl"])
    try:
        host_account = verify_role_tab_account(host_tab, "host")
    except RuntimeError as error:
        host_tab.close()
        raise SystemExit(f"Refusing to join: {error}") from error
    holder: dict[str, Any] = {
        "tab": host_tab,
        "host_account": host_account,
        "url": str(tab_info.get("url") or "").split("?")[0],
        "tab_id": tab_info.get("id"),
        "sso_accounts": signed_sso_accounts,
        "sso_accounts_scanned_at": time.time(),
        "sso_required_roles": list(required_roles),
        "sso_role_authusers": dict(args.role_authusers),
        "sso_role_emails": dict(args.role_emails),
        "sso_verified_roles": {"host": host_account},
        "sso_satisfied": False,
        "sso_satisfied_at": None,
        "sso_resolved_accounts": {},
        "sso_rescan_permitted": False,
        "sso_consent_operation_id": f"bridge-account-scan:{uuid.uuid4().hex}",
        "companion_click_enabled": bool(args.companion_click),
        "companion_click_interval_seconds": float(args.companion_click_interval),
        "companion_click_mode": str(args.companion_click_mode),
        "companion_click_trigger": str(args.companion_click_trigger),
        "companion_click_after_seconds": float(args.companion_click_after),
        "companion_click_silence_ms": float(args.companion_click_silence_ms),
        "companion_click_min_gap_seconds": float(args.companion_click_min_gap),
        "companion_click_max_wait_seconds": float(args.companion_click_max_wait),
        "companion_click_audio_rms_threshold": float(args.companion_click_audio_rms_threshold),
        "companion_click_ms": float(args.companion_click_ms),
        "companion_click_gain": float(args.companion_click_gain),
        "companion_click_sound": str(args.companion_click_sound),
        "companion_click_phrase": str(args.companion_click_phrase or args.companion_click_sound),
        "companion_click_f0_hz": float(args.companion_click_f0),
        "companion_click_f1_hz": float(args.companion_click_f1),
        "companion_click_f2_hz": float(args.companion_click_f2),
        "companion_click_meeting_url": None,
        "companion_click_source": "default",
        "companion_click_installed": False,
        "companion_click_last_click_at": None,
        "companion_click_last_install_at": None,
        "companion_click_last_error": None,
        "companion_clicks_sent": 0,
        "companion_click_suppressed": 0,
        "companion_click_row_breaks_observed": 0,
        "companion_click_pending_breaks": [],
        "companion_click_artifact_until": 0.0,
        "companion_click_last_trigger": None,
        "companion_click_last_trigger_mode": None,
        "companion_click_last_trigger_reason": None,
        "companion_click_last_trigger_at": None,
        "companion_click_last_trigger_iso": None,
        "companion_click_last_phrase": None,
        "companion_click_current_silence_ms": None,
        "companion_click_eligibility": "disabled" if not args.companion_click else "waiting-for-companion",
        "companion_click_companion_ready": False,
        "companion_click_queue": {"queued": 0, "speaking": False, "currentKind": None},
        "companion_click_schedule_started_monotonic": time.monotonic(),
        "companion_click_last_silence_ms": None,
        "companion_click_last_monologue_seconds": None,
        "companion_state": "not-attached",
        "companion_mic_ready": False,
        "companion_heard_stt_enabled": bool(args.companion_heard_stt),
        "companion_heard_stt_output_device": str(args.companion_listen_device or ""),
        "companion_heard_stt_input_device_selector": str(args.companion_heard_stt_input_device or ""),
        "companion_heard_stt_input_device_id": None,
        "companion_heard_stt_input_device_name": None,
        "companion_heard_stt_capture_attempted": False,
        "companion_heard_stt_capture_last_attempt_at": None,
        "companion_heard_stt_capture_listening": False,
        "companion_heard_stt_capture_live": False,
        "companion_heard_stt_sink_status": None,
        "companion_heard_stt_sink_device_label": None,
        "companion_heard_stt_last_error": None,
        "companion_heard_stt_tap_status": "disabled",
        "companion_heard_stt_tap_connected": False,
        "companion_heard_stt_media_muted": True,
        "companion_heard_stt_stream_id": None,
        "companion_heard_stt_sample_rate": None,
        "companion_heard_stt_chunks_captured": 0,
        "companion_heard_stt_frames_captured": 0,
        "companion_heard_stt_bytes_captured": 0,
        "companion_heard_stt_chunks_forwarded": 0,
        "companion_heard_stt_frames_forwarded": 0,
        "companion_heard_stt_bytes_forwarded": 0,
        "companion_heard_stt_chunks_dropped": 0,
        "companion_heard_stt_frames_dropped": 0,
        "companion_heard_stt_bytes_dropped": 0,
        "companion_heard_stt_transport_chunks_dropped": 0,
        "companion_heard_stt_transport_frames_dropped": 0,
        "companion_heard_stt_transport_bytes_dropped": 0,
        "companion_heard_stt_artifact_chunks_suppressed": 0,
        "companion_heard_stt_disconnects": 0,
        "companion_heard_stt_reconnects": 0,
        "companion_heard_stt_server_capture": None,
        "companion_say_artifact_started_at": 0.0,
        "companion_say_artifact_until": 0.0,
    }
    update_sso_satisfaction(
        holder,
        role_authusers=args.role_authusers,
        role_emails=args.role_emails,
        required_roles=required_roles,
    )
    meeting_url = str(tab_info.get("url") or "").split("?")[0]
    print(f"[bridge] attached: {tab_info.get('title', '')!r} {meeting_url}", flush=True)

    def announce(text_line: str, metadata: dict[str, Any] | None = None) -> None:
        for recipient in recipients:
            try:
                mailbox.send(recipient, text_line, sender="meet-bridge", metadata=metadata or {"source": "google-meet-bridge"})
            except Exception as error:  # noqa: BLE001
                print(f"[mailbox] announce failed: {error}", file=sys.stderr, flush=True)

    def nav_tab_id(role: str) -> str:
        value = holder.get("tab_id") if role == "host" else holder.get(f"{role}_tab_id")
        return str(value or "")

    def logged_location_href(tab: CdpTab, target_url: str, *, role: str, reason: str, detail: str) -> None:
        navigator.evaluate_location_href(
            tab,
            target_url,
            cdp_endpoint=cdp_endpoint,
            reason=reason,
            detail=detail,
            role=role,
            component="meet_bridge",
            tab_id=nav_tab_id(role),
            intended_identity=role_email(role),
            effective_identity=str((holder.get(f"{role}_account") or {}).get("email") or "") or None,
        )

    def logged_location_reload(tab: CdpTab, target_url: str, *, role: str, reason: str, detail: str) -> None:
        navigator.evaluate_location_reload(
            tab,
            target_url,
            cdp_endpoint=cdp_endpoint,
            reason=reason,
            detail=detail,
            role=role,
            component="meet_bridge",
            tab_id=nav_tab_id(role),
            intended_identity=role_email(role),
            effective_identity=str((holder.get(f"{role}_account") or {}).get("email") or "") or None,
        )

    if created_servant and "meet.google.com" in meeting_url and "/new" not in meeting_url:
        print(f"[bridge] servant meeting created: {meeting_url}", flush=True)
        announce(
            f"Servant meeting is up: {meeting_url} -- I sit in it alone and transcribe the room mic. "
            "You do NOT need to join; invite me elsewhere with '/join <meet-url>' (or /new).",
            {"source": "google-meet-bridge", "meetingUrl": meeting_url, "servant": True},
        )

    stop = threading.Event()

    def whoami(tab: CdpTab | None) -> dict[str, Any] | None:
        return read_google_account(tab)

    def cached_sso_accounts() -> list[dict[str, Any]]:
        return list(cached_sso_accounts_status(holder)["ssoAccounts"])

    def scan_sso_accounts_now(*, allow_scan: bool = False) -> list[dict[str, Any]]:
        nonlocal signed_sso_accounts
        try:
            accounts = scan_sso_accounts_if_permitted(
                holder,
                lambda: scan_signed_in_sso_accounts(
                    cdp_endpoint,
                    authusers=sso_probe_authusers(args.role_authusers),
                    reason="account-scan",
                    detail="Meet bridge is refreshing cached SSO account identities after a tab/account change",
                    role="probe",
                    component="meet_bridge",
                    sso_satisfied=bool(holder.get("sso_satisfied")),
                    consent_operation_id=str(holder["sso_consent_operation_id"]),
                ),
                allow_scan=allow_scan,
                role_authusers=args.role_authusers,
                role_emails=args.role_emails,
                required_roles=required_roles,
            )
        except navigator.NavigationBlockedError as error:
            log(f"[bridge] account scan stopped before probing: {error}", role="probe")
            return list(holder.get("sso_accounts") or [])
        signed_sso_accounts = list(accounts)
        return accounts

    def record_role_verified(role: str, account: dict[str, Any]) -> None:
        verified = dict(holder.get("sso_verified_roles") or {})
        verified[role] = dict(account)
        holder["sso_verified_roles"] = verified
        update_sso_satisfaction(
            holder,
            role_authusers=args.role_authusers,
            role_emails=args.role_emails,
            required_roles=required_roles,
        )

    def _host_profile_info() -> dict[str, Any]:
        """Which Chrome profile dir (and therefore which persisted Google SSO
        login) the HOST tab is using."""
        if args.attach_only:
            return {
                "path": None,
                "known": False,
                "label": "unknown (attached externally via --cdp; profile not chosen by this bridge)",
                "account": {"label": "unknown -- no live window to check", "signedIn": False, "email": None},
            }
        path = str(Path(args.profile).expanduser())
        account = holder.get("host_account") or whoami(holder.get("tab")) or {"label": "unknown -- no live window to check", "signedIn": False, "email": None}
        if holder.get("tab") is not None:
            holder["host_account"] = account
        return {"path": path, "known": True, "label": path, "account": account, "authuser": role_authuser("host")}

    # ---- STT-subsystem integration: /health + /captions for consumers ------
    # `captionCount` = distinct stored rows (add/edit collapses to one per
    # key); `emitCount` = total raw emit() calls ever made (every add AND
    # every edit counted separately).
    holder["host_process"] = host_process
    holder["host_profile"] = str(Path(args.profile).expanduser()) if not args.attach_only else None
    holder["companion_process"] = None
    status: dict[str, Any] = {
        "ok": True,
        "service": "ws_collab_meet_bridge",
        "meetingUrl": holder.get("url"),
        "lastCaptionAt": None,
        "captionCount": 0,
        "emitCount": 0,
        "outbox": args.outbox,
        "recipients": recipients,
        "hostProfile": _host_profile_info(),
        "browserBackend": args.browser_backend,
        "rawText": "",
        "rawRows": [],
        "rawAt": None,
        "rawIso": None,
        "rawRowCount": 0,
        "rawChildCount": 0,
        "rawHistoryCount": 0,
    }
    update_companion_click_status(status, holder)
    update_companion_heard_stt_status(status, holder)
    status["rawByRole"] = {role: _blank_raw_caption_role() for role in CAPTION_ROLES}
    status["captionTransportByRole"] = {role: _blank_caption_transport_role() for role in CAPTION_ROLES}
    _refresh_caption_transport_state(status, active_roles=["host"])
    holder["rawText"] = ""
    holder["rawRows"] = []
    holder["rawAt"] = None
    holder["rawIso"] = None
    holder["rawRowCount"] = 0
    holder["rawChildCount"] = 0
    holder["rawHistory"] = []
    holder["rawByRole"] = {role: _blank_raw_caption_role() for role in CAPTION_ROLES}
    captions_log: list[dict[str, Any]] = []  # ring buffer for the ws_collab STT driver
    captions_index: dict[str, int] = {}  # row key -> index into captions_log, for in-place ADD/EDIT
    captions_lock = threading.Lock()
    trackers = {role: CaptionTracker(args.settle) for role in CAPTION_ROLES}
    caption_emitter = CaptionEmitter(
        holder=holder,
        status=status,
        captions_log=captions_log,
        captions_index=captions_index,
        captions_lock=captions_lock,
        mailbox=mailbox,
        recipients=recipients,
        ignore=ignore,
        self_name=args.self_name,
        sender_prefix=args.sender_prefix,
    )
    # Live per-room snapshot, keyed by room id (e.g. "bgb-xqts-xjt") -- one
    # entry per DRIVER meeting this bridge has ever been in, holding
    # "as of last time we were there" host/companion profile+state. Never
    # pruned (bounded implicitly: only a handful of driver rooms exist).
    # A future CLIENT/GUEST meeting (the bridge just sitting in someone
    # else's call, not one of its own servant meetings) would be keyed and
    # snapshotted the exact same way -- no separate structure needed.
    meeting_state: dict[str, dict[str, Any]] = {}

    # ---- debug/status ring buffer -- "other things" the admin UI can show
    # beside captions: autojoin verdicts, mic-select attempts, dialog
    # handling, /say and /join outcomes.
    debug_log: list[dict[str, Any]] = []
    debug_lock = threading.Lock()

    def log(text: str, *, err: bool = False, role: str = "bridge") -> None:
        """`role` is which controlled identity this line is about --
        "host", "companion", or "bridge" for cross-cutting/whole-process
        events (join/new-meeting, autojoin, caption-DOM parsing -- all of
        which act on the HOST tab, but aren't really "about" the host
        identity the way a mic-select/mute/\"/say\" line is). Surfaced to the
        admin UI's debug table as a Source column so it's clear at a glance
        which browser window a given line came from."""
        print(text, file=sys.stderr if err else None, flush=True)
        with debug_lock:
            debug_log.append({"at": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"), "text": text, "role": role})
            del debug_log[:-200]

    def attach_cdp_logging(tab: Any, role: str) -> None:
        setter = getattr(tab, "set_error_handler", None)
        if setter is not None:
            setter(lambda text, role=role: log(f"[cdp] {text}", err=True, role=role))

    click_settings_dir = settings_dir
    click_profile = Path(args.profile).expanduser()

    def _normalize_click_setting(raw: Any, default: dict[str, Any]) -> dict[str, Any]:
        row = raw if isinstance(raw, dict) else {}
        enabled = bool(row.get("enabled", default.get("enabled", False)))
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
            raw_value = next((row[key] for key in aliases[name] if key in row), default.get(name, fallback))
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                value = float(default.get(name, fallback))
            return value if value > 0 else float(default.get(name, fallback))

        def nonnegative(name: str, fallback: float) -> float:
            raw_value = next((row[key] for key in aliases[name] if key in row), default.get(name, fallback))
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                value = float(default.get(name, fallback))
            return value if value >= 0 else float(default.get(name, fallback))

        mode = str(
            row.get("mode") or row.get("triggerMode") or row.get("trigger_mode")
            or default.get("mode") or "reactive"
        ).lower().replace("-", "_")
        mode = {"on_silence": "reactive", "interval": "fixed"}.get(mode, mode)
        if mode not in {"reactive", "fixed"}:
            mode = "reactive"
        trigger = str(row.get("trigger") or default.get("trigger") or "caption").lower()
        if trigger not in {"caption", "audio", "both"}:
            trigger = "caption"
        sound = str(row.get("sound") or default.get("sound") or "uh").lower()
        if sound not in {"uh", "click"}:
            sound = "uh"
        phrase = str(row.get("phrase") or row.get("sound") or default.get("phrase") or sound).lower()
        if phrase not in {"uh", "uhuh", "hmm", "click"}:
            phrase = "uh"
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
            "phrase": phrase,
            "f0Hz": positive("f0Hz", 125.0),
            "f1Hz": positive("f1Hz", 600.0),
            "f2Hz": positive("f2Hz", 1300.0),
        }

    def _read_companion_click_setting(target_url: str | None) -> dict[str, Any]:
        key = meeting_key(target_url)
        default = {
            "enabled": bool(args.companion_click),
            "intervalSeconds": float(args.companion_click_interval),
            "mode": str(args.companion_click_mode),
            "trigger": str(args.companion_click_trigger),
            "afterSeconds": float(args.companion_click_after),
            "silenceMs": float(args.companion_click_silence_ms),
            "minGapSeconds": float(args.companion_click_min_gap),
            "maxWaitSeconds": float(args.companion_click_max_wait),
            "audioRmsThreshold": float(args.companion_click_audio_rms_threshold),
            "clickMs": float(args.companion_click_ms),
            "gain": float(args.companion_click_gain),
            "sound": str(args.companion_click_sound),
            "phrase": str(args.companion_click_phrase or args.companion_click_sound),
            "f0Hz": float(args.companion_click_f0),
            "f1Hz": float(args.companion_click_f1),
            "f2Hz": float(args.companion_click_f2),
        }
        try:
            state = MeetBrowserSettings(click_settings_dir).get_profile_state(click_profile)
        except Exception as error:  # noqa: BLE001
            _log_companion_click(log, "settings-read", f"[click] settings read failed: {error}", err=True, interval=10.0)
            state = {}
        default_setting = _normalize_click_setting(state.get("companion_click"), default)
        overrides = state.get("meeting_companion_click", {})
        if key and isinstance(overrides, dict) and isinstance(overrides.get(key), dict):
            setting = _normalize_click_setting(overrides.get(key), default_setting)
            source = "override"
        else:
            setting = default_setting
            source = "default"
        return {**setting, "meetingUrl": key, "source": source}

    def _persist_companion_click_override(target_url: str, enabled: bool, interval_seconds: float | None = None) -> dict[str, Any]:
        key = meeting_key(target_url)
        if not key:
            raise ValueError("expected a Meet URL or room id")
        store = MeetBrowserSettings(click_settings_dir)
        state = store.get_profile_state(click_profile)
        overrides = state.get("meeting_companion_click", {})
        overrides = dict(overrides) if isinstance(overrides, dict) else {}
        current = _read_companion_click_setting(key)
        interval = float(interval_seconds if interval_seconds is not None else current.get("intervalSeconds", 2.0))
        overrides[key] = {**current, "enabled": bool(enabled), "intervalSeconds": interval}
        overrides[key].pop("meetingUrl", None)
        overrides[key].pop("source", None)
        known = list(state.get("known_meeting_urls") or [])
        if key not in known:
            known.append(key)
        store.set_profile_state(click_profile, meeting_companion_click=overrides, known_meeting_urls=known)
        return {"enabled": bool(enabled), "intervalSeconds": interval, "meetingUrl": key, "source": "override"}

    def _current_companion_click_target() -> str | None:
        return meeting_key(holder.get("url"))

    def sync_companion_click_for_meeting(reason: str, *, force: bool = False) -> dict[str, Any]:
        setting = _read_companion_click_setting(holder.get("url"))
        signature = (
            bool(setting["enabled"]),
            float(setting["intervalSeconds"]),
            str(setting.get("mode") or "reactive"),
            str(setting.get("trigger") or "caption"),
            float(setting.get("afterSeconds") or 10.0),
            float(setting.get("silenceMs") or 500.0),
            float(setting.get("minGapSeconds") or 6.0),
            float(setting.get("maxWaitSeconds") or 0.0),
            float(setting.get("audioRmsThreshold") or 0.015),
            float(setting.get("clickMs") or 100.0),
            float(setting.get("gain") or 0.12),
            str(setting.get("sound") or "uh"),
            str(setting.get("phrase") or setting.get("sound") or "uh"),
            float(setting.get("f0Hz") or 125.0),
            float(setting.get("f1Hz") or 600.0),
            float(setting.get("f2Hz") or 1300.0),
            setting.get("meetingUrl"),
            setting.get("source"),
        )
        changed = force or holder.get("companion_click_signature") != signature
        holder["companion_click_enabled"] = bool(setting["enabled"])
        holder["companion_click_interval_seconds"] = float(setting["intervalSeconds"])
        holder["companion_click_mode"] = str(setting.get("mode") or "reactive")
        holder["companion_click_trigger"] = str(setting.get("trigger") or "caption")
        holder["companion_click_after_seconds"] = float(setting.get("afterSeconds") or 10.0)
        holder["companion_click_silence_ms"] = float(setting.get("silenceMs") or 500.0)
        holder["companion_click_min_gap_seconds"] = float(setting.get("minGapSeconds") or 6.0)
        holder["companion_click_max_wait_seconds"] = float(setting.get("maxWaitSeconds") or 0.0)
        holder["companion_click_audio_rms_threshold"] = float(setting.get("audioRmsThreshold") or 0.015)
        holder["companion_click_ms"] = float(setting.get("clickMs") or 100.0)
        holder["companion_click_gain"] = float(setting.get("gain") or 0.12)
        holder["companion_click_sound"] = str(setting.get("sound") or "uh")
        holder["companion_click_phrase"] = str(setting.get("phrase") or setting.get("sound") or "uh")
        holder["companion_click_f0_hz"] = float(setting.get("f0Hz") or 125.0)
        holder["companion_click_f1_hz"] = float(setting.get("f1Hz") or 600.0)
        holder["companion_click_f2_hz"] = float(setting.get("f2Hz") or 1300.0)
        holder["companion_click_meeting_url"] = setting.get("meetingUrl")
        holder["companion_click_source"] = setting.get("source")
        if changed:
            holder["companion_click_schedule_started_monotonic"] = time.monotonic()
            holder.pop("companion_click_fired_silence_event", None)
        holder["companion_click_signature"] = signature
        update_companion_click_status(status, holder)
        if changed:
            state_text = "enabled" if setting["enabled"] else "disabled"
            _log_companion_click(
                log,
                f"sync-{reason}",
                f"[click] companion ticker {state_text} for {setting.get('meetingUrl') or 'unknown meeting'} ({setting['source']}, {setting['intervalSeconds']:g}s)",
                interval=0.0,
            )
            tab = holder.get("companion_tab")
            if tab is not None:
                apply_companion_click_state(tab, holder, status, log=log)
                if time.time() >= float(holder.get("speaking_until") or 0):
                    try:
                        tab.evaluate(autojoin_js("speaking" if setting["enabled"] else "muted"))
                    except Exception as error:  # noqa: BLE001
                        _log_companion_click(log, "sync-mic", f"[click] companion mic-policy failed: {error}", err=True, interval=10.0)
        return setting

    def companion_click_verdict() -> str:
        click = update_companion_click_status(status, holder)
        state = "on" if click["enabled"] else "off"
        last = click.get("lastClickIso") or "never"
        return f"click:{state} meeting={click.get('meetingUrl') or 'unknown'} source={click.get('source')} trigger={click.get('trigger')} silence={click['silenceMs']:g}ms interval={click['intervalSeconds']:g}s last={last}"

    def set_companion_click(enabled: bool, interval_seconds: float | None = None, target_url: str | None = None) -> str:
        target = meeting_key(target_url) or _current_companion_click_target()
        if not target:
            return "click failed: no current Meet room"
        try:
            saved = _persist_companion_click_override(target, enabled, interval_seconds)
        except Exception as error:  # noqa: BLE001
            return f"click failed: {error}"
        current = _current_companion_click_target()
        if current == saved["meetingUrl"]:
            sync_companion_click_for_meeting("command", force=True)
        else:
            state_text = "enabled" if enabled else "disabled"
            _log_companion_click(
                log,
                "saved-other",
                f"[click] companion ticker {state_text} for {saved['meetingUrl']} ({saved['intervalSeconds']:g}s)",
                interval=0.0,
            )
        return f"click:{'on' if enabled else 'off'} meeting={saved['meetingUrl']}"

    attach_cdp_logging(holder["tab"], "host")
    install_caption_push(holder["tab"], role="host", log=log)
    sync_companion_click_for_meeting("startup", force=True)

    def _controlled_clients() -> list[dict[str, Any]]:
        """Every participant the bridge actively drives, and which device
        stands in for their mic/speaker -- HOST is real hardware and is
        never automated, so it is deliberately not listed here (its own
        profile/SSO info is on the top-level status as "hostProfile"
        instead)."""
        clients: list[dict[str, Any]] = []
        if args.companion:
            companion_account = holder.get("companion_account")
            if holder.get("companion_tab") is not None:
                companion_account = whoami(holder.get("companion_tab")) or companion_account
                if companion_account:
                    holder["companion_account"] = companion_account
            mic = args.mic_select_device or "(WebAudio synthetic mic patch)"
            if args.mic_select_device and not holder.get("companion_mic_confirmed"):
                mic += " -- attempting, not yet confirmed by Meet"
            speak = (f"device #{tts_output_device_index} (virtual cable)" if tts_output_device_index is not None
                     else "(WebAudio synthetic speaker patch)")
            clients.append({
                "role": "companion",
                "state": "in-call" if holder.get("companion_tab") else "not-yet-joined",
                "mic": mic,
                "speak": speak,
                "companionClick": update_companion_click_status(status, holder),
                "companionHeardStt": update_companion_heard_stt_status(status, holder),
                "companionAudio": companion_audio.status(),
                # Set by companion_loop() when it attaches the companion tab.
                "profile": holder.get("companion_profile"),
                "account": companion_account or {"label": "unknown -- no live window to check", "signedIn": False, "email": None},
                "authuser": role_authuser("companion"),
            })
        return clients

    def _process_info(role: str, process: subprocess.Popen[Any] | None, *, port: int, profile: Path | str | None, backend: str) -> dict[str, Any]:
        return {
            "role": role,
            "pid": process.pid if process else None,
            "alive": (process.poll() is None) if process else None,
            "port": port,
            "profile": str(profile) if profile else None,
            "backend": backend,
        }

    def _tracked_processes() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        host_proc = holder.get("host_process")
        if host_proc is not None or not args.attach_only:
            rows.append(_process_info("host", host_proc, port=args.port, profile=holder.get("host_profile"), backend=args.browser_backend))
        return rows


    def _snapshot_current_meeting_state() -> None:
        """Record host/companion profile+state for whichever room we're
        CURRENTLY in, keyed by its room id -- called every main-loop tick
        (cheap: a couple of dict/list builds) plus right after switch_to()
        so a fresh join is reflected immediately rather than waiting for
        the next poll. This is what lets meeting_state[room] answer "what
        was HOST/COMPANION doing here?" even after the bridge has since
        moved to a different meeting."""
        room = room_id(holder.get("url"))
        if not room:
            return
        meeting_state[room] = {
            "url": holder.get("url"),
            "updatedAt": time.time(),
            "hostProfile": _host_profile_info(),
            "clients": _controlled_clients(),
        }

    def _companion_audio_readiness(target_url: str | None) -> dict[str, Any]:
        active = meeting_key(holder.get("url"))
        requested = meeting_key(target_url) if target_url else active
        tab_id = holder.get("companion_tab_id")
        ready = bool(
            args.companion
            and holder.get("companion_tab") is not None
            and tab_id
            and holder.get("companion_state") == "in-call"
            and holder.get("companion_mic_ready")
            and active
            and requested == active
        )
        error = None
        if not args.companion:
            error = "bridge was not started with a companion"
        elif requested != active:
            error = f"requested meeting {requested or 'none'} is not active ({active or 'none'})"
        elif holder.get("companion_tab") is None or not tab_id:
            error = "companion tab is not attached"
        elif holder.get("companion_state") != "in-call":
            error = f"companion is not in-call ({holder.get('companion_state') or 'unknown'})"
        elif not holder.get("companion_mic_ready"):
            error = "companion synthetic microphone is not ready"
        return {
            "ready": ready,
            "meetingUrl": active,
            "tabId": tab_id,
            "state": holder.get("companion_state"),
            "syntheticMicReady": bool(holder.get("companion_mic_ready")),
            "error": error,
        }

    def _cancel_companion_audio(_reason: str) -> None:
        tab = holder.get("companion_tab")
        if tab is not None:
            tab.evaluate(CANCEL_COMPANION_AUDIO_JS, timeout=3)

    companion_audio = CompanionAudioArbiter(
        _companion_audio_readiness,
        lambda item, cancel_event: _play_companion_audio(item, cancel_event),
        _cancel_companion_audio,
        max_pending=args.companion_audio_queue_max,
    )

    def _health_server() -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlparse

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path.rstrip("/") == "/captions":
                    qs = parse_qs(parsed.query)
                    since = 0.0
                    try:
                        since = float((qs.get("since") or ["0"])[0])
                    except ValueError:
                        since = 0.0

                    from_end = None
                    if "fromEnd" in qs:
                        try:
                            from_end = int(qs["fromEnd"][0])
                        except ValueError:
                            pass

                    now = time.time()
                    active_caption_roles = ["host", *(["companion"] if holder.get("companion_tab") is not None else [])]
                    with captions_lock:
                        transport_by_role = _refresh_caption_transport_state(status, active_roles=active_caption_roles, now=now)
                        # Filter by `updated_at`, not `at` (creation time) --
                        # a row can be EDITED in place long after it was
                        # first added (Meet revising it), and a poller
                        # needs to see that edit even though the row's
                        # creation time is older than their `since` cursor.
                        rows = [row for row in captions_log if row["updated_at"] > since]
                        if from_end is not None:
                            rows = rows[-from_end:] if from_end > 0 else []
                        meetings = sorted({row.get("meetingUrl") for row in captions_log if row.get("meetingUrl")})
                        raw_text = str(holder.get("rawText") or "")
                        raw_rows = list(holder.get("rawRows") or [])
                        raw_at = holder.get("rawAt")
                        raw_iso = holder.get("rawIso")
                        raw_row_count = holder.get("rawRowCount")
                        raw_child_count = holder.get("rawChildCount")
                        raw_history = list(holder.get("rawHistory") or [])
                        caption_transport = status.get("captionTransport", "poll")
                        last_push_at = status.get("lastPushAt")
                        last_push_iso = status.get("lastPushIso")
                        push_frame_count = status.get("pushFrameCount", 0)
                        caption_transport_by_role = {
                            role_name: dict(role_state)
                            for role_name, role_state in transport_by_role.items()
                        }
                    raw_by_role = {}
                    for raw_role, raw_state in (holder.get("rawByRole") or {}).items():
                        if isinstance(raw_state, dict):
                            raw_by_role[raw_role] = {
                                **raw_state,
                                "rawRows": list(raw_state.get("rawRows") or []),
                                "rawHistory": list(raw_state.get("rawHistory") or []),
                            }
                    companion_click = update_companion_click_status(status, holder)
                    companion_heard_stt = update_companion_heard_stt_status(status, holder)
                    body = json.dumps({
                        "captions": rows, "now": time.time(), "meetingUrl": holder.get("url"), "meetings": meetings,
                        "rawText": raw_text, "rawRows": raw_rows, "rawAt": raw_at, "rawIso": raw_iso,
                        "rawRowCount": raw_row_count, "rawChildCount": raw_child_count, "rawHistory": raw_history,
                        "rawByRole": raw_by_role, "captionTransport": caption_transport,
                        "lastPushAt": last_push_at, "lastPushIso": last_push_iso,
                        "pushFrameCount": push_frame_count, "captionTransportByRole": caption_transport_by_role,
                        "companionClick": companion_click,
                        "companionHeardStt": companion_heard_stt,
                        "companionAudio": companion_audio.status(),
                    }).encode("utf-8")
                else:
                    with debug_lock:
                        debug_rows = list(debug_log[-50:])
                    _snapshot_current_meeting_state()
                    host_profile = _host_profile_info()
                    status["hostProfile"] = host_profile
                    update_companion_click_status(status, holder)
                    update_companion_heard_stt_status(status, holder)
                    status["companionAudio"] = companion_audio.status()
                    with captions_lock:
                        _refresh_caption_transport_state(
                            status,
                            active_roles=["host", *(["companion"] if holder.get("companion_tab") is not None else [])],
                        )
                    body = json.dumps({
                        **status,
                        "meetingUrl": holder.get("url"),
                        "hostProfile": host_profile,
                        "clients": _controlled_clients(),
                        **cached_sso_accounts_status(holder),
                        "debug": debug_rows, "meetingState": meeting_state, "processes": _tracked_processes(),
                    }).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("access-control-allow-origin", "*")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:  # noqa: N802
                # CORS preflight -- needed for every request the admin UI
                # makes, not just POST /command: its shared api() helper
                # always attaches an Authorization header (meant for
                # ws_collab's own API), which turns even a plain GET
                # /health or /captions into a "non-simple" request that the
                # browser preflights first.
                self.send_response(204)
                self.send_header("access-control-allow-origin", "*")
                self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
                self.send_header("access-control-allow-headers", "content-type, authorization")
                self.send_header("content-length", "0")
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                # POST /command {"command": "/join <url>" | "/new" | "/say <text>" | "/click on"}
                # -- lets a UI (the ws_collab admin's Google Meet page) drive
                # the bridge directly over HTTP.
                parsed = urlparse(self.path)
                route = parsed.path.rstrip("/")
                if route not in {"/command", "/speech", "/speech/cancel", "/speech/status"}:
                    self.send_response(404)
                    self.send_header("access-control-allow-origin", "*")
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                try:
                    length = int(self.headers.get("content-length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    payload = json.loads(raw or b"{}")
                    if route == "/speech/status":
                        utterance_id = str(payload.get("utterance_id") or "").strip()
                        wait_seconds = float(payload.get("wait_seconds") or 0.0)
                        body_obj = companion_audio.utterance_status(
                            utterance_id, wait_seconds=wait_seconds
                        )
                        status_code = 200 if body_obj.get("ok") else 404
                    elif route == "/speech/cancel":
                        utterance_id = str(payload.get("utterance_id") or "").strip()
                        cancelled = bool(utterance_id and companion_audio.cancel(utterance_id))
                        body_obj = {"ok": True, "cancelled": cancelled, "utterance_id": utterance_id}
                        status_code = 200
                    elif route == "/speech":
                        destination = str(payload.get("destination") or "companion").strip().lower()
                        if destination != "companion":
                            body_obj = {
                                "ok": False,
                                "accepted": False,
                                "error": "bridge speech destination must be 'companion'",
                            }
                        else:
                            body_obj = companion_audio.submit(
                                kind="speech",
                                text=str(payload.get("text") or ""),
                                meeting_url=payload.get("meeting_url"),
                                source=str(payload.get("artifact_source") or "virtual-agent-tts"),
                                metadata={
                                    "utterance_id": payload.get("utterance_id"),
                                    "agent_id": payload.get("agent_id"),
                                    "correlation_id": payload.get("correlation_id"),
                                    "voice_id": payload.get("voice_id"),
                                    "requested_voice_id": payload.get("requested_voice_id"),
                                    "rate": payload.get("rate", 1.0),
                                    "pitch": payload.get("pitch", 0.0),
                                    "volume": payload.get("volume", 1.0),
                                },
                            )
                        status_code = 202 if body_obj.get("ok") else 409
                    else:
                        command = str(payload.get("command") or "").strip()
                        verdict = handle_command(command) if command else "empty-command"
                        if verdict is None:
                            verdict = "unrecognized-command"
                        ok = not str(verdict).startswith("say-rejected:")
                        status_code, body_obj = (200 if ok else 409), {"ok": ok, "verdict": verdict}
                except Exception as error:  # noqa: BLE001
                    status_code, body_obj = 400, {"ok": False, "error": str(error)}
                body = json.dumps(body_obj).encode("utf-8")
                self.send_response(status_code)
                self.send_header("content-type", "application/json")
                self.send_header("access-control-allow-origin", "*")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:  # silence request spam
                return

        try:
            server = ThreadingHTTPServer(("127.0.0.1", args.status_port), Handler)
        except OSError as error:
            print(f"[status] health port {args.status_port} unavailable: {error}", file=sys.stderr, flush=True)
            return
        print(f"[status] health endpoint: http://127.0.0.1:{args.status_port}/health", flush=True)
        server.timeout = 1.0
        while not stop.is_set():
            server.handle_request()
        server.server_close()

    if args.status_port:
        threading.Thread(target=_health_server, daemon=True).start()

    # ---- presence companion: a second SSO keeps the meeting populated ------
    # It can also TALK: /say and /click route audio through its synthetic mic.
    def companion_loop() -> None:
        companion_cdp = cdp_endpoint
        companion_profile = Path(args.profile).expanduser()
        # Stashed on `holder` (not just this closure's locals) so
        # _controlled_clients() (SSO/profile display) and disconnect_browsers()
        # (needs the tab id to hang up over CDP) can reach them from outside
        # this thread without recomputing/duplicating the derivation above.
        holder["companion_cdp"] = companion_cdp
        holder["companion_profile"] = str(companion_profile)
        told_sso = False
        told_waiting = False
        companion_tab: CdpTab | None = None
        # Synthetic-mic patch state for the CURRENT tab/JS-realm. A full
        # page navigations wipe window.* state, so this
        # must be re-applied any time the realm resets.
        mic_ready = False
        reloaded_for_mic = False
        mic_selected = False
        # HANDS OFF until the operator has signed in and joined the call
        # themselves once -- no navigation, no clicks, nothing that could
        # yank the window away mid-sign-in. Automation begins only after the
        # first in-call sighting.
        operator_joined = False
        cdp_was_alive = False
        while not stop.is_set():
            target = str(holder.get("url") or "")
            if "meet.google.com" not in target:
                stop.wait(3)
                continue
            sync_companion_click_for_meeting("poll")
            try:
                if not cdp_alive(companion_cdp):
                    if cdp_was_alive:
                        invalidate_sso_satisfaction(holder, "companion-cdp-disconnected")
                        cdp_was_alive = False
                    if not told_waiting:
                        told_waiting = True
                        print("[companion] waiting for the browser process to expose its DevTools port...", flush=True)
                    stop.wait(3)
                    continue
                if not cdp_was_alive and holder.get("sso_rescan_permitted"):
                    scan_sso_accounts_now(allow_scan=True)
                cdp_was_alive = True
                told_waiting = False
                if not told_sso:
                    told_sso = True
                    print("[companion] using its assigned authuser tab in the browser profile", flush=True)
                if companion_tab is not None and holder.get("companion_tab_id") is None:
                    companion_audio.invalidate("companion tab closed")
                    invalidate_sso_satisfaction(holder, "companion-tab-closed", clear_roles=("companion",))
                    companion_tab = None
                    holder["companion_state"] = "not-attached"
                    holder["companion_mic_ready"] = False
                    holder["companion_click_installed"] = False
                    update_companion_click_status(status, holder)
                info = find_role_meet_tab(
                    companion_cdp,
                    "companion",
                    wanted_room=target if operator_joined else None,
                )
                if not info:
                    reusable_tab = find_role_meet_tab(companion_cdp, "companion")
                    if reusable_tab is None:
                        log(
                            f"[companion] no existing browser page for {role_email('companion')}; "
                            f"creating one with authuser={role_authuser('companion')}",
                            role="companion",
                        )
                    else:
                        log(
                            f"[companion] reusing browser page for {role_email('companion')}: "
                            f"{reusable_tab.get('url')}",
                            role="companion",
                        )
                    if companion_tab is not None:
                        companion_audio.invalidate("companion tab relaunched")
                        invalidate_sso_satisfaction(holder, "companion-tab-relaunched", clear_roles=("companion",))
                        companion_tab.close()
                    info, reused_tab = reuse_or_open_tab(
                        companion_cdp,
                        role_target_url(target, "companion"),
                        existing_in_scope=reusable_tab,
                        navigate_existing=reusable_tab is not None,
                        reason="join-meeting",
                        detail=(
                            f"companion connector for {role_email('companion')} is joining "
                            f"{target} so Meet keeps two participants present"
                        ),
                        role="companion",
                        component="meet_bridge",
                        intended_identity=role_email("companion"),
                    )
                    companion_tab = None
                    holder["companion_tab"] = None
                    holder["companion_tab_id"] = None
                    holder["companion_state"] = "not-attached"
                    holder["companion_mic_ready"] = False
                    holder["companion_click_installed"] = False
                    update_companion_click_status(status, holder)
                    if not operator_joined and not reused_tab:
                        print("[companion] authuser tab opened -- sign in with the assigned account if needed; I wait for the first in-call sighting before taking over.", flush=True)
                    if not (info and info.get("webSocketDebuggerUrl")):
                        stop.wait(3)
                        continue
                if companion_tab is None:
                    info, _ = reuse_or_open_tab(
                        companion_cdp,
                        str(info.get("url") or ""),
                        existing_in_scope=info,
                        reason="reattach-lost-tab",
                        detail="companion tab was found but local CDP handle was missing; reattaching and foregrounding it",
                        role="companion",
                        component="meet_bridge",
                        intended_identity=role_email("companion"),
                    )
                    if not (info and info.get("webSocketDebuggerUrl")):
                        stop.wait(3)
                        continue
                    companion_tab = CdpTab(info["webSocketDebuggerUrl"])
                    attach_cdp_logging(companion_tab, "companion")
                    if holder.get("sso_rescan_permitted"):
                        scan_sso_accounts_now(allow_scan=True)
                    try:
                        holder["companion_account"] = verify_role_tab_account(companion_tab, "companion")
                    except RuntimeError as error:
                        invalidate_sso_satisfaction(holder, "companion-verification-failed", clear_roles=("companion",))
                        scan_sso_accounts_now(allow_scan=True)
                        companion_tab.close()
                        companion_tab = None
                        holder["companion_tab"] = None
                        holder["companion_tab_id"] = None
                        holder["companion_click_installed"] = False
                        update_companion_click_status(status, holder)
                        log(f"[companion] refusing to join: {error}", err=True, role="companion")
                        return
                    record_role_verified("companion", holder["companion_account"])
                    holder["companion_tab"] = companion_tab
                    holder["companion_tab_id"] = info.get("id")
                    holder["companion_state"] = "attached"
                    install_caption_push(companion_tab, role="companion", log=log)
                    mic_ready = False
                    reloaded_for_mic = False
                    mic_selected = False
                    holder["companion_click_installed"] = False
                    holder["companion_click_last_error"] = None
                    update_companion_click_status(status, holder)
                # Install the synthetic-mic patch ASAP so Meet's own
                # getUserMedia calls (prejoin preview, mic toggles) resolve
                # to our WebAudio destination instead of the real hardware.
                # Idempotent in JS (window.__sapiPatched guard).
                if not mic_ready or holder.get("companion_click_enabled") or holder.get("companion_click_installed"):
                    try:
                        companion_tab.evaluate(GUM_PATCH_JS)
                        mic_ready = True
                        holder["companion_mic_ready"] = True
                    except Exception as error:  # noqa: BLE001
                        mic_ready = False
                        holder["companion_mic_ready"] = False
                        print(f"[companion] mic patch failed: {error}", file=sys.stderr, flush=True)
                if mic_ready and (holder.get("companion_click_enabled") or holder.get("companion_click_installed")):
                    apply_companion_click_state(companion_tab, holder, status, log=log)
                # Hands-off applies to SIGN-IN, not to joining: if the tab
                # shows a meet page with a Join/Rejoin button the SSO is
                # proven and the companion may (re)join unattended.
                state = str(companion_tab.evaluate(
                    "document.querySelector('button[aria-label*=\"eave call\" i]') ? 'in-call'"
                    " : location.hostname.includes('accounts.google') ? 'signin'"
                    " : [...document.querySelectorAll('button')].some(b => /join now|ask to join|join anyway|rejoin/i.test((b.textContent||'') + (b.getAttribute('aria-label')||''))) ? 'prejoin-ready'"
                    " : 'elsewhere'"))
                holder["companion_state"] = state
                if state == "in-call" and not operator_joined:
                    operator_joined = True
                    log(
                        "[companion] you're in -- taking over: "
                        + "staying muted and deaf; remote audio is tapped without speaker playback when enabled.",
                        role="companion",
                    )
                if state == "signin" and not operator_joined:
                    if not told_waiting:
                        told_waiting = True
                        print("[companion] waiting for YOU to sign in in the second window (no automation on sign-in pages)...", flush=True)
                    stop.wait(4)
                    continue
                if state == "elsewhere":
                    # Signed in (this is meet.google.com, not a Google
                    # sign-in page) but not looking at our room -- e.g. still
                    # on the post-leave-call home screen. Safe to steer there
                    # regardless of operator_joined: no OAuth flow to disturb.
                    logged_location_href(
                        companion_tab,
                        role_target_url(target, "companion"),
                        role="companion",
                        reason="join-meeting",
                        detail=f"companion tab was signed in but not at {target}; steering it to the meeting",
                    )
                    mic_ready = False
                    holder["companion_mic_ready"] = False
                    reloaded_for_mic = False
                    mic_selected = False
                    holder["companion_click_installed"] = False
                    update_companion_click_status(status, holder)
                    stop.wait(4)
                    continue
                if state == "prejoin-ready" and mic_ready and not reloaded_for_mic:
                    # Meet may have already grabbed a real-mic stream for its
                    # local preview before the patch landed. One reload here
                    # (still pre-join, so nothing live is disrupted) guarantees
                    # the very next getUserMedia call sees the patched version.
                    reloaded_for_mic = True
                    mic_ready = False
                    holder["companion_mic_ready"] = False
                    holder["companion_click_installed"] = False
                    update_companion_click_status(status, holder)
                    logged_location_reload(
                        companion_tab,
                        str(info.get("url") or role_target_url(target, "companion")),
                        role="companion",
                        reason="join-meeting",
                        detail="reload companion prejoin page so the synthetic-mic patch applies before joining",
                    )
                    stop.wait(3)
                    continue
                if operator_joined and target.split("?")[0] not in str(info.get("url") or ""):
                    logged_location_href(
                        companion_tab,
                        role_target_url(target, "companion"),
                        role="companion",
                        reason="switch-meeting",
                        detail=f"companion had joined but drifted away from {target}; navigating back to the assigned room",
                    )
                    mic_ready = False
                    holder["companion_mic_ready"] = False
                    reloaded_for_mic = False
                    mic_selected = False
                    holder["companion_click_installed"] = False
                    update_companion_click_status(status, holder)
                    stop.wait(4)
                    continue
                # If a real virtual cable is configured, point Meet's own mic
                # dropdown at it (once per session) so Meet actually captures
                # from the cable instead of real hardware or the WebAudio
                # patch. No-op when --mic-select-device is unset.
                if state == "in-call" and args.mic_select_device and not mic_selected:
                    try:
                        mic_verdict = companion_tab.evaluate(SELECT_MIC_DEVICE_JS % json.dumps(args.mic_select_device.lower()))
                        log(f"[companion] mic-select: {mic_verdict}", role="companion")
                        if isinstance(mic_verdict, str) and mic_verdict.startswith("mic-selected:"):
                            mic_selected = True
                            holder["companion_mic_confirmed"] = True
                    except Exception as error:  # noqa: BLE001
                        log(f"[companion] mic-select failed: {error}", err=True, role="companion")
                # While say_into_meeting() owns the mic, don't fight it with
                # a mute click. If the companion clicker is enabled, keep the
                # same fake-mic-only "speaking" policy active so Meet transmits
                # the synthetic ticks.
                if time.time() >= float(holder.get("speaking_until") or 0):
                    mic_policy = "speaking" if holder.get("companion_click_enabled") else "muted"
                    verdict = companion_tab.evaluate(autojoin_js(mic_policy))
                    if verdict == "unmuted-for-speech" and holder.get("companion_click_enabled"):
                        _log_companion_click(log, "unmuted", "[click] companion unmuted for synthetic ticker", interval=60.0)
                    if verdict in ("join-clicked", "stayed-in-call", "muted", "admitted"):
                        log(f"[companion] {verdict}", role="companion")
                if state == "in-call" and holder.get("companion_click_enabled"):
                    audio_probe = None
                    now_mono = time.monotonic()
                    mode = str(holder.get("companion_click_mode") or "reactive")
                    if mode != "fixed":
                        if str(holder.get("companion_click_trigger") or "caption") in {"audio", "both"}:
                            audio_probe = measure_companion_audio_silence(companion_tab, holder, status, log=log)
                    decision = companion_interjection_decision(holder, now=now_mono, audio_probe=audio_probe)
                    queued = queue_companion_interjection(
                        holder,
                        companion_audio,
                        decision,
                        meeting_url=holder.get("url"),
                        now=now_mono,
                    )
                    if queued.get("accepted"):
                        _log_companion_click(
                            log,
                            "reactive-click",
                            (
                                f"[backchannel] queued companion "
                                f"{holder.get('companion_click_phrase') or 'uh'!r} "
                                f"via {decision.get('trigger')}"
                            ),
                            interval=10.0,
                        )
                    update_companion_click_status(status, holder)
                if state == "in-call" and args.companion_heard_stt:
                    forward_companion_heard_audio(companion_tab, mailbox, holder, status, log=log)
                else:
                    companion_tab.evaluate('document.querySelectorAll("audio,video").forEach((m) => { m.muted = true; m.volume = 0; })')
            except Exception as error:  # noqa: BLE001
                invalidate_sso_satisfaction(holder, "companion-cdp-error", clear_roles=("companion",))
                companion_tab = None
                holder["companion_tab"] = None
                holder["companion_tab_id"] = None
                holder["companion_state"] = "not-attached"
                holder["companion_mic_ready"] = False
                companion_audio.invalidate("companion CDP attachment lost")
                holder["companion_click_installed"] = False
                update_companion_click_status(status, holder)
                log(f"[companion] {error}", err=True, role="companion")
                stop.wait(3)
            stop.wait(3)

    def _play_companion_audio(item: dict[str, Any], cancel_event: threading.Event) -> None:
        """Play one already-arbitrated speech/interject item on its bound tab."""

        tab = holder.get("companion_tab")
        if tab is None or holder.get("companion_tab_id") != item.get("tabId"):
            raise RuntimeError("companion tab changed before playback")
        if item.get("kind") == "interject":
            decision = (item.get("metadata") or {}).get("decision") or {}
            phrase = str((item.get("metadata") or {}).get("phrase") or "uh")
            if not trigger_companion_click(
                tab,
                holder,
                status,
                prior_host_key=str(decision.get("rowKey") or ""),
                trigger_reason=str(decision.get("trigger") or "interject"),
                silence_ms=decision.get("silenceMs"),
                monologue_seconds=decision.get("monologueSeconds"),
                phrase=phrase,
                log=log,
            ):
                raise RuntimeError(str(holder.get("companion_click_last_error") or "interject failed"))
            duration_ms = float(holder.get("companion_click_ms") or 100.0)
            if phrase == "uhuh":
                duration_ms = max(duration_ms, 280.0)
            elif phrase == "hmm":
                duration_ms = max(duration_ms, 220.0)
            cancel_event.wait(max(0.001, duration_ms / 1000.0))
            return

        metadata = item.get("metadata") or {}
        b64, duration = sapi_wav_base64(
            str(item.get("text") or ""),
            voice_id=str(metadata.get("voice_id") or ""),
            rate=float(metadata.get("rate") or 1.0),
            pitch=float(metadata.get("pitch") or 0.0),
            volume=float(metadata.get("volume") if metadata.get("volume") is not None else 1.0),
        )
        companion_audio.report_duration(str(item.get("id") or ""), duration)
        if cancel_event.is_set() or not _companion_audio_readiness(item.get("meetingUrl")).get("ready"):
            raise RuntimeError("companion speech cancelled before playback")
        artifact_started = time.time()
        holder["companion_say_artifact_started_at"] = artifact_started
        holder["companion_say_artifact_until"] = artifact_started + duration + 2.0
        holder["companion_say_artifact"] = {
            "id": item.get("id"),
            "source": item.get("source"),
            "agentId": metadata.get("agent_id"),
            "correlationId": metadata.get("correlation_id"),
            "expectedText": item.get("text"),
            "meetingUrl": item.get("meetingUrl"),
        }
        holder["speaking_until"] = holder["companion_say_artifact_until"]
        try:
            unmute_verdict = tab.evaluate(autojoin_js("speaking"))
            if cancel_event.wait(0.3):
                raise RuntimeError("companion speech cancelled before playback")
            if tts_output_device_index is not None:
                import base64 as _base64

                play_wav_bytes_to_device(
                    _base64.b64decode(b64),
                    tts_output_device_index,
                    cancellation=cancel_event,
                )
                verdict = f"completed-via-device-{tts_output_device_index}"
            else:
                verdict = tab.evaluate(
                    SPEAK_INTO_MEETING_JS % json.dumps(b64),
                    await_promise=True,
                    timeout=max(30, int(duration + 10)),
                )
            if not isinstance(verdict, str) or not verdict.startswith("completed"):
                raise RuntimeError(str(verdict or "synthetic microphone playback failed"))
            log(f"[say] {unmute_verdict}/{verdict}: {str(item.get('text') or '')[:80]}", role="companion")
        finally:
            holder["speaking_until"] = 0.0
            if holder.get("companion_tab_id") == item.get("tabId"):
                try:
                    tab.evaluate(autojoin_js("speaking" if holder.get("companion_click_enabled") else "muted"))
                except Exception as error:  # noqa: BLE001
                    log(f"[say] re-mute failed: {error}", err=True, role="companion")

    def say_into_meeting(text: str, *, meeting_url: str | None = None) -> dict[str, Any]:
        """/say <text>: queue SAPI speech through the companion synthetic mic.

        Never touches the real host mic -- this only works once --companion
        is running, has joined, and its getUserMedia patch has landed (or,
        with --tts-output-device configured, once Meet's mic dropdown is
        pointed at a virtual cable's recording side). Also doubles as the
        captioning self-test: speak known text through the companion and
        verify Google's own captions (Emit/Phrases/Transcribe) come back
        matching it.
        """
        result = companion_audio.submit(
            kind="speech",
            text=text,
            meeting_url=meeting_url,
            source="legacy-say",
            metadata={"agent_id": "legacy-say"},
        )
        if not result.get("accepted"):
            log(f"[say] rejected: {result.get('error')}", err=True, role="companion")
        return result

    companion_audio.start()
    if args.companion:
        threading.Thread(target=companion_loop, daemon=True).start()
        print("[companion] armed: a muted second authuser tab in the browser will sit in the meeting so Google keeps it alive", flush=True)

    def switch_to(target_url: str | None) -> None:
        """Leave for another meeting: /join <url> or /new (fresh servant room)."""
        companion_audio.invalidate("meeting switch")
        old_id = holder.get("tab_id")
        old_url = str(holder.get("url") or "")
        host_target = role_target_url(target_url or "https://meet.google.com/new", "host")
        existing = next(
            (
                tab
                for tab in list_tabs(cdp_endpoint)
                if old_id and str(tab.get("id") or "") == str(old_id)
            ),
            None,
        )
        existing = existing or find_role_meet_tab(
            cdp_endpoint,
            "host",
            wanted_room=target_url,
        ) or find_role_meet_tab(cdp_endpoint, "host")
        navigate_existing = target_url is None or room_id(target_url) != room_id(old_url)
        try:
            info, reused_tab = reuse_or_open_tab(
                cdp_endpoint,
                host_target,
                existing_in_scope=existing,
                navigate_existing=bool(existing and navigate_existing),
                reason="new-meeting" if target_url is None else "switch-meeting",
                detail=(
                    f"host tab lost or switching from {old_url or 'unknown'} "
                    + ("to a freshly-created Meet room" if target_url is None else f"to {target_url}")
                ),
                role="host",
                component="meet_bridge",
                intended_identity=role_email("host"),
            )
        except Exception as error:  # noqa: BLE001
            print(f"[bridge] switch failed: could not reuse connector tab ({error})", file=sys.stderr, flush=True)
            return
        if navigate_existing or not reused_tab:
            deadline = time.time() + 600
            info = None
            while time.time() < deadline and not stop.is_set():
                try:
                    info = find_role_meet_tab(
                        cdp_endpoint,
                        "host",
                        require_room=True,
                        wanted_room=target_url,
                        exclude_room=old_url if target_url is None and reused_tab else None,
                    )
                except Exception:
                    info = None
                if info:
                    break
                time.sleep(1.5)
        if not info:
            print("[bridge] switch failed: connector tab did not reach the meeting", file=sys.stderr, flush=True)
            return
        invalidate_sso_satisfaction(holder, "host-tab-relaunched", clear_roles=("host",))
        scan_sso_accounts_now(allow_scan=True)
        old = holder.get("tab")
        holder["tab"] = CdpTab(info["webSocketDebuggerUrl"])
        attach_cdp_logging(holder["tab"], "host")
        try:
            holder["host_account"] = verify_role_tab_account(holder["tab"], "host")
        except RuntimeError as error:
            invalidate_sso_satisfaction(holder, "host-verification-failed", clear_roles=("host",))
            print(f"[bridge] switch failed: {error}", file=sys.stderr, flush=True)
            return
        record_role_verified("host", holder["host_account"])
        holder["tab_id"] = info.get("id")
        holder["url"] = str(info.get("url") or "").split("?")[0]
        sync_companion_click_for_meeting("switch", force=True)
        install_caption_push(holder["tab"], role="host", log=log)
        if old:
            try:
                old.close()
            except Exception:
                pass
        log(f"[bridge] now bridging: {holder['url']}", role="host")
        _snapshot_current_meeting_state()
        announce(f"Meet bridge moved -- now in: {holder['url']}", {"source": "google-meet-bridge", "meetingUrl": holder["url"]})

    def foreground_browsers(role: str | None = None) -> str:
        """/foreground [host|companion]: raise the browser window(s) this
        process is actually driving. With no role, raises every window it
        controls (HOST always, COMPANION if --companion is armed and has
        joined). 'guest' is accepted -- the connector table already
        anticipates a future GUEST/CLIENT identity -- but reports honestly
        that it isn't implemented yet rather than silently doing nothing.
        Reports exactly which window(s) it reached; a tab that has gone
        away (closed, crashed, never joined) is a normal, expected outcome
        to report, not an exception to hide."""
        wanted = (role or "").strip().lower() or None
        if wanted == "guest":
            return "foreground failed: guest/client tabs are not implemented yet"
        if wanted not in (None, "host", "companion"):
            return f"foreground failed: unknown role {wanted!r}"
        raised: list[str] = []
        failed: list[str] = []
        if wanted in (None, "host"):
            tab = holder.get("tab")
            if tab is not None:
                if args.browser_backend == "wsl":
                    failed.append("host (backend=wsl, no OS window to foreground by design)")
                else:
                    try:
                        navigator.foreground(
                            tab,
                            str(holder.get("url") or ""),
                            cdp_endpoint=cdp_endpoint,
                            reason="operator-foreground",
                            detail="operator requested foregrounding the host browser tab",
                            role="host",
                            component="meet_bridge",
                            tab_id=nav_tab_id("host"),
                            intended_identity=role_email("host"),
                            effective_identity=str((holder.get("host_account") or {}).get("email") or "") or None,
                            origin="operator",
                        )
                        raised.append("host")
                    except Exception as error:  # noqa: BLE001
                        failed.append(f"host ({error})")
            else:
                failed.append("host (no tab)")
        if wanted in (None, "companion"):
            companion_tab = holder.get("companion_tab")
            if companion_tab is not None:
                if args.browser_backend == "wsl":
                    failed.append("companion (backend=wsl, no OS window to foreground by design)")
                else:
                    try:
                        navigator.foreground(
                            companion_tab,
                            str(holder.get("url") or ""),
                            cdp_endpoint=cdp_endpoint,
                            reason="operator-foreground",
                            detail="operator requested foregrounding the companion browser tab",
                            role="companion",
                            component="meet_bridge",
                            tab_id=nav_tab_id("companion"),
                            intended_identity=role_email("companion"),
                            effective_identity=str((holder.get("companion_account") or {}).get("email") or "") or None,
                            origin="operator",
                        )
                        raised.append("companion")
                    except Exception as error:  # noqa: BLE001
                        failed.append(f"companion ({error})")
            elif args.companion:
                failed.append("companion (not joined yet)")
            elif wanted == "companion":
                failed.append("companion (armed with --companion first)")
        verdict = f"foregrounded:{'+'.join(raised) or 'none'}"
        if failed:
            verdict += f" (failed: {', '.join(failed)})"
        log(f"[bridge] {verdict}", role=(wanted or "bridge"))
        return verdict

    def open_sso_account(account: str | None = None) -> str:
        wanted = (account or "").strip().lower() or None
        if wanted == "add-account":
            target = "https://accounts.google.com/AccountChooser?continue=https://accounts.google.com/"
            existing = find_add_account_tab(cdp_endpoint)
            verdict = "sso:add-account"
            navigate_existing = existing is not None
        else:
            try:
                authuser = int(wanted) if wanted is not None else -1
            except ValueError:
                return f"sso failed: expected an authuser number, got {wanted!r}"
            if authuser < 0:
                return f"sso failed: expected a non-negative authuser number, got {wanted!r}"
            target = with_authuser("https://accounts.google.com/", authuser)
            account_info = next(
                (
                    entry
                    for entry in cached_sso_accounts()
                    if entry.get("authuser") == authuser
                ),
                None,
            )
            email = str((account_info or {}).get("email") or "")
            existing = find_sso_connector_tab(cdp_endpoint, email) if email else None
            verdict = f"sso:{authuser}"
            navigate_existing = False
        try:
            info, _ = reuse_or_open_tab(
                cdp_endpoint,
                target,
                existing_in_scope=existing,
                navigate_existing=navigate_existing,
                reason="add-account" if wanted == "add-account" else "operator-request",
                detail=(
                    "operator requested add-account SSO page in the bridge browser"
                    if wanted == "add-account"
                    else f"operator requested SSO authuser {authuser} in the bridge browser"
                ),
                role="server",
                component="meet_bridge",
                sso_intent=(
                    navigator.SsoIntent.ADD_ACCOUNT
                    if wanted == "add-account"
                    else navigator.SsoIntent.OPERATOR_REQUEST
                ),
                intended_identity=None if wanted == "add-account" else email,
                origin="operator",
            )
        except Exception as error:  # noqa: BLE001
            return f"{verdict} failed: could not reuse account page ({error})"
        if not (info and info.get("webSocketDebuggerUrl")):
            return f"{verdict} failed: account page did not open"
        holder["sso_accounts_scanned_at"] = 0.0
        log(f"[bridge] {verdict}", role="bridge")
        return verdict

    def kill_process(role: str | None = None) -> str:
        wanted = (role or "").strip().lower() or None
        if wanted == "guest":
            return "kill failed: guest/client tabs are not implemented yet"
        if wanted not in ("host", "companion"):
            return f"kill failed: unknown role {wanted!r}"
        key = f"{wanted}_process"
        process = holder.get(key)
        if process is None:
            return f"kill failed: no process tracked for {wanted} (attach-only mode, or never launched)"
        ok = _terminate_process(process)
        if ok:
            invalidate_sso_satisfaction(holder, f"{wanted}-process-killed")
            holder[key] = process
            if wanted == "host":
                holder["tab"] = None
                holder["tab_id"] = None
                holder["url"] = None
            else:
                companion_audio.invalidate("companion process killed")
                holder["companion_tab"] = None
                holder["companion_tab_id"] = None
                holder["companion_state"] = "not-attached"
                holder["companion_mic_ready"] = False
                holder["companion_click_installed"] = False
                update_companion_click_status(status, holder)
            verdict = f"killed:{wanted}"
        else:
            verdict = f"kill failed: {wanted} did not exit"
        log(f"[bridge] {verdict}", role=wanted)
        return verdict

    def disconnect_browsers(role: str | None = None) -> str:
        """/disconnect [host|companion] (+ alias /hangup): hang up by
        closing the browser tab(s) this process controls, over CDP -- the
        same "/json/close" mechanism switch_to() already uses to hang up a
        meeting we're leaving. HOST recovers on its own afterwards: the
        main caption-poll loop already treats a lost tab as a normal event
        (reattaches if the operator rejoins that window, or after 20s spins
        up a fresh servant meeting when not --attach-only) -- exactly like a
        real dropped call, no special-case recovery needed here. COMPANION
        simply stops being tracked; companion_loop() relaunches it next
        tick if --companion is still armed. 'guest' is honestly not
        implemented, matching foreground_browsers()."""
        wanted = (role or "").strip().lower() or None
        if wanted == "guest":
            return "disconnect failed: guest/client tabs are not implemented yet"
        if wanted not in (None, "host", "companion"):
            return f"disconnect failed: unknown role {wanted!r}"
        closed: list[str] = []
        failed: list[str] = []
        if wanted in (None, "host"):
            tab = holder.get("tab")
            tab_id = holder.get("tab_id")
            if tab is not None and tab_id:
                invalidate_sso_satisfaction(holder, "host-tab-disconnected", clear_roles=("host",))
                close_tab(cdp_endpoint, tab_id)
                try:
                    tab.close()
                except Exception:
                    pass
                holder["tab"] = None
                holder["tab_id"] = None
                holder["url"] = None
                closed.append("host")
            else:
                failed.append("host (no tab)")
        if wanted in (None, "companion"):
            companion_tab = holder.get("companion_tab")
            companion_tab_id = holder.get("companion_tab_id")
            companion_cdp = holder.get("companion_cdp")
            if companion_tab is not None and companion_tab_id and companion_cdp:
                companion_audio.invalidate("companion disconnected")
                invalidate_sso_satisfaction(holder, "companion-tab-disconnected", clear_roles=("companion",))
                close_tab(companion_cdp, companion_tab_id)
                try:
                    companion_tab.close()
                except Exception:
                    pass
                holder["companion_tab"] = None
                holder["companion_tab_id"] = None
                holder["companion_state"] = "not-attached"
                holder["companion_mic_ready"] = False
                holder["companion_click_installed"] = False
                update_companion_click_status(status, holder)
                closed.append("companion")
            elif args.companion:
                failed.append("companion (not joined yet)")
            elif wanted == "companion":
                failed.append("companion (armed with --companion first)")
        verdict = f"disconnected:{'+'.join(closed) or 'none'}"
        if failed:
            verdict += f" (failed: {', '.join(failed)})"
        log(f"[bridge] {verdict}", role=(wanted or "bridge"))
        return verdict

    def handle_command(command: str) -> str | None:
        """Recognize /join <url>, /new (+ aliases /meet /servant), /say
        <text>, /click on|off, /foreground [host|companion] (+ alias /focus),
        and /disconnect [host|companion] (+ alias /hangup); return a short
        verdict string if `command` was one of those and has been acted on, or
        None if it isn't a recognized control command. Shared by the
        mailbox-driven out_loop and the bridge's own HTTP /command endpoint so
        both paths behave identically.
        """
        lowered = command.lower()
        if lowered.startswith("/join"):
            parts = command.split(None, 1)
            target = parts[1].strip() if len(parts) > 1 else None
            switch_to(target)
            return f"joined:{target}" if target else "new-servant-meeting"
        if lowered in ("/new", "/meet", "/servant"):
            switch_to(None)
            return "new-servant-meeting"
        if lowered.startswith("/say"):
            parts = command.split(None, 1)
            spoken = parts[1].strip() if len(parts) > 1 else ""
            if spoken:
                queued = say_into_meeting(spoken)
                if queued.get("accepted"):
                    return "speaking"
                return f"say-rejected:{queued.get('reason')}:{queued.get('error')}"
            return "say-empty"
        click_command = parse_companion_click_command(command)
        if click_command is not None:
            action = click_command.get("action")
            if action == "invalid":
                return f"click failed: {click_command.get('error')}"
            if not args.companion:
                return "click failed: start with --companion"
            if action == "status":
                if click_command.get("meetingUrl"):
                    setting = _read_companion_click_setting(click_command.get("meetingUrl"))
                    state = "on" if setting["enabled"] else "off"
                    return f"click:{state} meeting={setting.get('meetingUrl')} source={setting.get('source')} interval={setting['intervalSeconds']:g}s"
                return companion_click_verdict()
            if action == "on":
                return set_companion_click(True, click_command.get("intervalSeconds"), click_command.get("meetingUrl"))
            if action == "off":
                return set_companion_click(False, target_url=click_command.get("meetingUrl"))
        if lowered.startswith("/foreground") or lowered.startswith("/focus"):
            parts = command.split(None, 1)
            role = parts[1].strip() if len(parts) > 1 else None
            return foreground_browsers(role)
        if lowered.startswith("/disconnect") or lowered.startswith("/hangup"):
            parts = command.split(None, 1)
            role = parts[1].strip() if len(parts) > 1 else None
            return disconnect_browsers(role)
        if lowered.startswith("/kill-process"):
            parts = command.split(None, 1)
            role = parts[1].strip() if len(parts) > 1 else None
            return kill_process(role)
        if lowered.startswith("/sso-scan"):
            invalidate_sso_satisfaction(holder, "operator-sso-scan", clear_verified=False)
            accounts = scan_sso_accounts_now(allow_scan=True)
            verdict = f"sso-scan:{len(accounts)}"
            log(f"[bridge] {verdict}", role="bridge")
            return verdict
        if lowered.startswith("/sso"):
            parts = command.split(None, 1)
            account = parts[1].strip() if len(parts) > 1 else None
            return open_sso_account(account)
        return None

    def out_loop() -> None:
        """ws_collab mailbox -> Meet chat (+ optional TTS), plus /join and
        /new commands."""
        while not stop.is_set():
            try:
                messages = mailbox.receive_new(args.outbox, limit=50)
            except Exception as error:  # noqa: BLE001
                print(f"[outbox] receive failed: {error}", file=sys.stderr, flush=True)
                messages = []
            for message in messages:
                text = message_text(message)
                if not text:
                    continue
                command = text.strip()
                if handle_command(command) is not None:
                    continue
                sender = str(message.get("from") or message.get("sender") or "workbench")
                line = f"[{sender}] {text}"
                try:
                    tab = holder["tab"]
                    verdict = tab.evaluate(SEND_CHAT_JS_TEMPLATE % json.dumps(line))
                    if verdict == "opened-chat-retry":
                        time.sleep(1.0)
                        verdict = tab.evaluate(SEND_CHAT_JS_TEMPLATE % json.dumps(line))
                    print(f"[meet-chat] {verdict}: {line[:80]}", flush=True)
                except Exception as error:  # noqa: BLE001
                    print(f"[meet-chat] failed: {error}", file=sys.stderr, flush=True)
                if args.speak:
                    threading.Thread(target=speak_windows, args=(text,), daemon=True).start()
            stop.wait(1.5)

    if not args.no_out:
        threading.Thread(target=out_loop, daemon=True).start()
        tts_note = " + TTS" if args.speak else ""
        print(f"[bridge] OUT armed: mailbox '{args.outbox}' -> Meet chat{tts_note} (commands: /join <url>, /new, /say <text>, /click on|off)", flush=True)
    print(f"[bridge] IN armed: captions -> mailbox {recipients}", flush=True)
    if not args.no_autojoin:
        print("[bridge] unattended: I click Join, keep the mic ON, and turn captions on myself.", flush=True)

    warned = ""
    autojoin_at = 0.0
    last_autojoin_verdict = ""
    lost_since: float | None = None
    fallback_logged_keys: set[str] = set()
    next_poll_at = 0.0
    push_reinstall_at = {role: 0.0 for role in CAPTION_ROLES}
    try:
        while True:
            tab = holder["tab"]
            active_roles = ["host", *(["companion"] if holder.get("companion_tab") is not None else [])]
            for role, role_tab in (("host", tab), ("companion", holder.get("companion_tab"))):
                if role_tab is None:
                    continue
                drain_caption_push_events(
                    role,
                    role_tab,
                    holder=holder,
                    status=status,
                    tracker=trackers[role],
                    caption_emitter=caption_emitter,
                    captions_lock=captions_lock,
                    fallback_logged_keys=fallback_logged_keys,
                    log=log,
                )
            with captions_lock:
                transport_by_role = _refresh_caption_transport_state(status, active_roles=active_roles, log=log)
            now = time.time()
            for role, role_tab in (("host", tab), ("companion", holder.get("companion_tab"))):
                if role_tab is None:
                    continue
                if transport_by_role.get(role, {}).get("captionTransport") == "push":
                    continue
                if now < push_reinstall_at.get(role, 0.0):
                    continue
                push_reinstall_at[role] = now + CAPTION_PUSH_REINSTALL_SECONDS
                install_caption_push(role_tab, role=role, log=log)
            if time.time() >= next_poll_at:
                try:
                    payloads = read_caption_payloads(holder, log=log)
                    lost_since = None
                except Exception as error:  # noqa: BLE001
                    print(f"[bridge] tab lost ({error}); reattaching?", file=sys.stderr, flush=True)
                    time.sleep(2.0)
                    info = find_role_meet_tab(cdp_endpoint, "host", wanted_room=holder.get("url"))
                    if info:
                        invalidate_sso_satisfaction(holder, "host-tab-reconnected", clear_roles=("host",))
                        scan_sso_accounts_now(allow_scan=True)
                        try:
                            tab.close()
                        except Exception:
                            pass
                        holder["tab"] = CdpTab(info["webSocketDebuggerUrl"])
                        attach_cdp_logging(holder["tab"], "host")
                        try:
                            holder["host_account"] = verify_role_tab_account(holder["tab"], "host")
                        except RuntimeError as verify_error:
                            invalidate_sso_satisfaction(holder, "host-verification-failed", clear_roles=("host",))
                            print(f"[bridge] refusing reattached host tab: {verify_error}", file=sys.stderr, flush=True)
                            continue
                        record_role_verified("host", holder["host_account"])
                        holder["tab_id"] = info.get("id")
                        holder["url"] = str(info.get("url") or "").split("?")[0]
                        sync_companion_click_for_meeting("reattach", force=True)
                        install_caption_push(holder["tab"], role="host", log=log)
                        lost_since = None
                    elif not args.attach_only:
                        lost_since = lost_since or time.time()
                        if time.time() - lost_since > 20:
                            print("[bridge] meeting gone -- creating a fresh servant meeting...", flush=True)
                            lost_since = None
                            switch_to(None)
                    continue
                note = ""
                for role, payload in payloads:
                    role_note = apply_caption_payload(
                        role,
                        payload,
                        holder=holder,
                        status=status,
                        tracker=trackers[role],
                        caption_emitter=caption_emitter,
                        captions_lock=captions_lock,
                        fallback_logged_keys=fallback_logged_keys,
                        log=log,
                        transport="poll",
                    )
                    if role == "host":
                        note = role_note
                if note and note != warned:
                    warned = note
                    print(f"[bridge] {note}", flush=True)
                with captions_lock:
                    transport_by_role = _refresh_caption_transport_state(status, active_roles=active_roles, log=log)
                    push_healthy = all(
                        transport_by_role.get(role, {}).get("captionTransport") == "push"
                        for role in active_roles
                    )
                poll_interval = max(args.poll, CAPTION_PUSH_POLL_INTERVAL) if push_healthy else args.poll
                next_poll_at = time.time() + poll_interval
            # Unattended servant behavior: join + enable captions ourselves.
            # Always tick (not gated on caption "quiet"/"ok" state) --
            # autojoin_js is idempotent and harmlessly returns "in-call" when
            # there's nothing to do.
            if not args.no_autojoin and time.time() - autojoin_at > 2.5:
                autojoin_at = time.time()
                try:
                    verdict = tab.evaluate(autojoin_js("keep"))
                    if verdict not in ("in-call", "waiting-prejoin") and verdict != last_autojoin_verdict:
                        log(f"[bridge] autojoin: {verdict}", role="host")
                    last_autojoin_verdict = verdict
                except Exception as error:  # noqa: BLE001
                    log(f"[bridge] autojoin failed: {error}", err=True, role="host")
            stop.wait(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        try:
            holder["tab"].close()
        except Exception:
            pass
        print("[bridge] stopped", flush=True)


if __name__ == "__main__":
    main()
