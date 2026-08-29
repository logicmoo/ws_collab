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

(the operator still clicks "Join now" in the popup window; the bridge
re-attaches automatically and posts where it went).

Nothing here logs into Google: you join the meeting normally in a Chrome
window this process pops up with its own remote-debugging port, then the
bridge attaches over the DevTools protocol (CDP) and reads/writes the page.

Two-bot design (task: HOST + COMPANION): Google ends/nags a meeting with a
single silent participant. `--companion` keeps a SECOND signed-in Google
account (its own SSO profile, signed in once) sitting muted+deaf in the call
so Google always sees two participants; the real (HOST) account's mic is
never touched by any automation.

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
from pathlib import Path
from typing import Any

from .audio_out import (
    list_audio_devices,
    play_wav_bytes_to_device,
    resolve_audio_device,
    sapi_wav_base64,
    speak_windows,
)
from .cdp import (
    DEFAULT_CDP,
    DEFAULT_PROFILE,
    CdpTab,
    build_launch,
    cdp_alive,
    close_tab,
    companion_profile_path,
    find_browser,
    find_meet_tab,
    launch_browser,
    list_tabs,
    open_url,
    wait_for_meet_tab,
)
from .mailbox_client import DEFAULT_BASE_URL as DEFAULT_MAILBOX_BASE
from .mailbox_client import MailboxClient
from .scripts_js import (
    CAPTIONS_JS,
    GUM_PATCH_JS,
    SELECT_MIC_DEVICE_JS,
    SEND_CHAT_JS_TEMPLATE,
    SPEAK_INTO_MEETING_JS,
    autojoin_js,
)
from .tracker import CaptionTracker

DEFAULT_RECIPIENTS = ["conversation"]
DEFAULT_SENDER_PREFIX = "meet-"
DEFAULT_OUTBOX = "google-meet"

# Meet's own room-id shape ("xxx-yyyy-zzz") -- the stable identity used to key
# per-room state (meeting_state below) regardless of whether that room is a
# HOST+COMPANION driver/servant meeting or (once built) a CLIENT/GUEST
# meeting the bridge just sits in: both are keyed the same way, uniformly.
_ROOM_RE = re.compile(r"meet\.google\.com/([a-z]{3,4}-[a-z]{3,5}-[a-z]{3,4})", re.IGNORECASE)


def room_id(url: str | None) -> str | None:
    match = _ROOM_RE.search(url or "")
    return match.group(1).lower() if match else None


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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cdp", default=os.environ.get("MEET_BRIDGE_CDP", DEFAULT_CDP), help="Chrome DevTools endpoint for attach mode (default %(default)s)")
    parser.add_argument("--meet", default=None, help="Google Meet URL -- pops up the bridge's own browser for SSO sign-in and joins there")
    parser.add_argument("--new", action="store_true", help="CREATE a new instant meeting (meet.google.com/new) for the signed-in account, join it, and post its link to the mailbox")
    parser.add_argument("--attach-only", action="store_true", help="Never pop a browser: only attach to an existing meet tab on --cdp")
    parser.add_argument("--companion", action="store_true", help="ALSO keep a second signed-in account (own SSO profile, one-time sign-in) sitting MUTED in the meeting so Google sees 2 participants and won't end/nag the servant meeting.")
    parser.add_argument("--companion-port", type=int, default=None, help="DevTools port for the companion browser (default --port + 1)")
    parser.add_argument("--status-port", type=int, default=48699, help="Local health/status HTTP port -- what ws_collab's google_meet STT driver and admin UI read (0 disables; default %(default)s)")
    parser.add_argument("--self-name", default="You", help="Name captions attribute to the bridge account's own mic (Meet shows 'You'; default %(default)s)")
    parser.add_argument("--no-autojoin", action="store_true", help="Do not auto-click Join/mic/captions -- drive the Meet window manually")
    parser.add_argument("--browser", default=None, help="Path to chrome.exe/msedge.exe for the popup (auto-detected)")
    parser.add_argument("--browser-backend", choices=["windows", "wsl"], default=os.environ.get("MEET_BRIDGE_BROWSER_BACKEND", "windows"), help="How to host the Chrome window(s): 'windows' (default, a normal visible window) or 'wsl' (runs inside WSL2 under a real Xvfb virtual display -- genuinely invisible on the Windows desktop, not just off-screen)")
    parser.add_argument("--wsl-distro", default=os.environ.get("MEET_BRIDGE_WSL_DISTRO"), help="WSL distro name for --browser-backend wsl (default: first distro from `wsl -l -q`)")
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE), help="Persistent profile dir for the popup browser (keeps your SSO login; default %(default)s)")
    parser.add_argument("--port", type=int, default=9223, help="DevTools port for the popup browser (default %(default)s)")
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
    parser.add_argument("--mic-select-device", default=os.environ.get("MEET_BRIDGE_MIC_SELECT_DEVICE"), help="Name (substring) of the device Meet's own Audio Settings mic dropdown should select, e.g. a virtual cable's 'Output' side -- omit to leave Meet's mic selection alone (env MEET_BRIDGE_MIC_SELECT_DEVICE)")
    args = parser.parse_args()

    if args.list_audio_devices:
        list_audio_devices()
        return

    tts_output_device_index: int | None = None
    if args.tts_output_device:
        try:
            tts_output_device_index = resolve_audio_device(args.tts_output_device, want="output")
            print(f"[bridge] /say will play through device #{tts_output_device_index} matching {args.tts_output_device!r}")
        except ValueError as error:
            raise SystemExit(f"--tts-output-device: {error}")

    if args.forget_sso:
        import shutil

        profile = Path(args.profile).expanduser()
        if cdp_alive(f"http://127.0.0.1:{args.port}"):
            raise SystemExit("Close the bridge browser window first, then rerun --forget-sso.")
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
            print(f"[bridge] SSO profile wiped: {profile} -- the next --meet run asks for the account again")
        else:
            print(f"[bridge] nothing to forget ({profile} does not exist)")
        return

    # SSO popup mode: unless --attach-only, the bridge owns a popup browser
    # (persistent SSO profile). Default with no --meet/--new: reuse a meeting
    # tab if one is open, otherwise CREATE the servant meeting.
    cdp_endpoint = args.cdp
    if not args.attach_only:
        cdp_endpoint, host_process = launch_browser(args)
    else:
        host_process = None

    if args.list_tabs:
        for tab_entry in list_tabs(cdp_endpoint):
            print(f"{tab_entry.get('type'):8} {tab_entry.get('title', '')[:60]!r} {tab_entry.get('url', '')[:90]}")
        return

    mailbox = MailboxClient(args.mailbox_base)
    recipients = args.to or list(DEFAULT_RECIPIENTS)
    ignore = {name.strip().lower() for name in args.ignore_speaker}

    created_servant = False
    if args.attach_only:
        tab_info = find_meet_tab(cdp_endpoint)
        if not tab_info:
            raise SystemExit(
                f"No meet.google.com tab found via {cdp_endpoint}.\n"
                "Either rerun without --attach-only (pops up an SSO browser window), or\n"
                "start Chrome with --remote-debugging-port=9222, join the Meet, then rerun."
            )
    else:
        tab_info = find_meet_tab(cdp_endpoint)
        if args.new or not tab_info:
            if tab_info is None and not args.meet:
                open_url(cdp_endpoint, "https://meet.google.com/new")
                created_servant = not args.new  # implicit servant meeting
            tab_info = wait_for_meet_tab(cdp_endpoint, require_room=True)
            created_servant = created_servant or args.new
        elif args.meet:
            tab_info = wait_for_meet_tab(cdp_endpoint)

    holder: dict[str, Any] = {"tab": CdpTab(tab_info["webSocketDebuggerUrl"]), "url": str(tab_info.get("url") or "").split("?")[0], "tab_id": tab_info.get("id")}
    meeting_url = str(tab_info.get("url") or "").split("?")[0]
    print(f"[bridge] attached: {tab_info.get('title', '')!r} {meeting_url}")

    def announce(text_line: str, metadata: dict[str, Any] | None = None) -> None:
        for recipient in recipients:
            try:
                mailbox.send(recipient, text_line, sender="meet-bridge", metadata=metadata or {"source": "google-meet-bridge"})
            except Exception as error:  # noqa: BLE001
                print(f"[mailbox] announce failed: {error}", file=sys.stderr)

    if created_servant and "meet.google.com" in meeting_url and "/new" not in meeting_url:
        print(f"[bridge] servant meeting created: {meeting_url}")
        announce(
            f"Servant meeting is up: {meeting_url} -- I sit in it alone and transcribe the room mic. "
            "You do NOT need to join; invite me elsewhere with '/join <meet-url>' (or /new).",
            {"source": "google-meet-bridge", "meetingUrl": meeting_url, "servant": True},
        )

    def emit(key: str, speaker: str, text: str, final: bool = False, replaces: str | None = None) -> None:
        if speaker.strip().lower() in ignore:
            return
        if speaker.strip().lower() in ("you", "sie", "tu", "vous"):
            speaker = args.self_name
        sender = args.sender_prefix + (re.sub(r"[^a-z0-9]+", "-", speaker.lower()).strip("-") or "speaker")
        line = f"{speaker}: {text}"
        meeting_url_now = holder.get("url")
        # Full info on every single emit -- never make a consumer look
        # anything up elsewhere: which key, whether it's a settled phrase
        # or still-growing speech, what key (if any) it continues from, and
        # which meeting it came from, every time, not just on the first
        # message for a given key.
        full_meta = {
            "source": "google-meet-captions", "speaker": speaker, "key": key,
            "final": final, "replaces": replaces, "meetingUrl": meeting_url_now,
        }
        for recipient in recipients:
            try:
                mailbox.send(recipient, line, sender=sender, metadata=dict(full_meta))
            except Exception as error:  # noqa: BLE001
                print(f"[mailbox] send failed: {error}", file=sys.stderr)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
        with captions_lock:
            # ADD the first time this row key is seen, EDIT/UPDATE it in
            # place (same position, refreshed `updated_at`) every time
            # after -- one evolving entry per utterance, not a growing pile
            # of overlapping near-duplicate lines. The consumer reassembles
            # the transcript by watching each key's latest text. `final`
            # marks a completed sentence ("phrase") that will never be
            # updated again -- a key only ever goes live->final once, so
            # writing it plainly here is safe (no key is reused after).
            idx = captions_index.get(key)
            if idx is not None and idx < len(captions_log) and captions_log[idx].get("key") == key:
                row = captions_log[idx]
                row.update({
                    "text": text, "updated_at": time.time(), "iso": now_iso,
                    "final": final, "replaces": replaces, "meetingUrl": meeting_url_now,
                })
            else:
                captions_log.append({
                    "key": key, "at": time.time(), "updated_at": time.time(), "iso": now_iso,
                    "speaker": speaker, "text": text, "meetingUrl": meeting_url_now,
                    "final": final, "replaces": replaces,
                })
                del captions_log[:-200]
                # Rebuild the index after any ring-buffer trim shifts positions
                # (bounded to 200 entries, so this is cheap).
                captions_index.clear()
                for i, row in enumerate(captions_log):
                    captions_index[row["key"]] = i
        status["captionCount"] = len(captions_log)
        status["emitCount"] = int(status.get("emitCount") or 0) + 1
        status["lastCaptionAt"] = now_iso
        print(f"[caption] {line}")

    tracker = CaptionTracker(args.settle)
    stop = threading.Event()

    def _host_profile_info() -> dict[str, Any]:
        """Which Chrome profile dir (and therefore which persisted Google SSO
        login) the HOST tab is using. Certain when the bridge launched its
        own popup browser (--profile pins one specific persistent profile
        dir); with --attach-only we connected to a Chrome the operator
        already had running and never chose -- or even saw -- its
        --user-data-dir, so say that plainly instead of guessing. Surfaced
        at the top of the admin UI's "Google Meet" page, next to the Bridge
        panel, plus reused per-row in the connector table."""
        if args.attach_only:
            return {"path": None, "known": False, "label": "unknown (attached externally via --cdp; profile not chosen by this bridge)"}
        path = str(Path(args.profile).expanduser())
        return {"path": path, "known": True, "label": path}

    # ---- STT-subsystem integration: /health + /captions for consumers ------
    # `captionCount` = distinct stored rows (add/edit collapses to one per
    # key); `emitCount` = total raw emit() calls ever made (every add AND
    # every edit counted separately).
    holder["host_process"] = host_process
    holder["host_profile"] = str(Path(args.profile).expanduser()) if not args.attach_only else None
    holder["companion_process"] = None
    status: dict[str, Any] = {"ok": True, "service": "ws_collab_meet_bridge", "meetingUrl": holder.get("url"), "lastCaptionAt": None, "captionCount": 0, "emitCount": 0, "outbox": args.outbox, "recipients": recipients, "hostProfile": _host_profile_info(), "browserBackend": args.browser_backend}
    captions_log: list[dict[str, Any]] = []  # ring buffer for the ws_collab STT driver
    captions_index: dict[str, int] = {}  # row key -> index into captions_log, for in-place ADD/EDIT
    captions_lock = threading.Lock()
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
        print(text, file=sys.stderr if err else None)
        with debug_lock:
            debug_log.append({"at": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"), "text": text, "role": role})
            del debug_log[:-200]

    def _controlled_clients() -> list[dict[str, Any]]:
        """Every participant the bridge actively drives, and which device
        stands in for their mic/speaker -- HOST is real hardware and is
        never automated, so it is deliberately not listed here (its own
        profile/SSO info is on the top-level status as "hostProfile"
        instead)."""
        clients: list[dict[str, Any]] = []
        if args.companion:
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
                # Set by companion_loop() the moment it computes its own
                # profile dir -- always known (the bridge always launches
                # this one itself, no attach-only equivalent for companion).
                "profile": holder.get("companion_profile"),
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
        if args.companion:
            rows.append(_process_info(
                "companion",
                holder.get("companion_process"),
                port=args.companion_port or (args.port + 1),
                profile=holder.get("companion_profile"),
                backend=args.browser_backend,
            ))
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

    def _health_server() -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlparse

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path.rstrip("/") == "/captions":
                    since = 0.0
                    try:
                        since = float((parse_qs(parsed.query).get("since") or ["0"])[0])
                    except ValueError:
                        since = 0.0
                    with captions_lock:
                        # Filter by `updated_at`, not `at` (creation time) --
                        # a row can be EDITED in place long after it was
                        # first added (Meet revising it), and a poller
                        # needs to see that edit even though the row's
                        # creation time is older than their `since` cursor.
                        rows = [row for row in captions_log if row["updated_at"] > since]
                        meetings = sorted({row.get("meetingUrl") for row in captions_log if row.get("meetingUrl")})
                    body = json.dumps({
                        "captions": rows, "now": time.time(), "meetingUrl": holder.get("url"), "meetings": meetings,
                    }).encode("utf-8")
                else:
                    with debug_lock:
                        debug_rows = list(debug_log[-50:])
                    _snapshot_current_meeting_state()
                    body = json.dumps({
                        **status, "meetingUrl": holder.get("url"), "clients": _controlled_clients(),
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
                # POST /command {"command": "/join <url>" | "/new" | "/say <text>"}
                # -- lets a UI (the ws_collab admin's Google Meet page) drive
                # the bridge directly over HTTP.
                parsed = urlparse(self.path)
                if parsed.path.rstrip("/") != "/command":
                    self.send_response(404)
                    self.send_header("access-control-allow-origin", "*")
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                try:
                    length = int(self.headers.get("content-length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    payload = json.loads(raw or b"{}")
                    command = str(payload.get("command") or "").strip()
                    verdict = handle_command(command) if command else "empty-command"
                    if verdict is None:
                        verdict = "unrecognized-command"
                    status_code, body_obj = 200, {"ok": True, "verdict": verdict}
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
            print(f"[status] health port {args.status_port} unavailable: {error}", file=sys.stderr)
            return
        print(f"[status] health endpoint: http://127.0.0.1:{args.status_port}/health")
        server.timeout = 1.0
        while not stop.is_set():
            server.handle_request()
        server.server_close()

    if args.status_port:
        threading.Thread(target=_health_server, daemon=True).start()

    # ---- presence companion: a second SSO keeps the meeting populated ------
    # It can also TALK: /say routes SAPI speech through its synthetic mic.
    speech_lock = threading.Lock()

    def companion_loop() -> None:
        companion_port = args.companion_port or (args.port + 1)
        companion_cdp = f"http://127.0.0.1:{companion_port}"
        companion_profile = companion_profile_path(Path(args.profile).expanduser())
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
        # navigation (reload, location.href=) wipes window.* state, so this
        # must be re-applied any time the realm resets.
        mic_ready = False
        reloaded_for_mic = False
        mic_selected = False
        # HANDS OFF until the operator has signed in and joined the call
        # themselves once -- no navigation, no clicks, nothing that could
        # yank the window away mid-sign-in. Automation begins only after the
        # first in-call sighting.
        operator_joined = False
        while not stop.is_set():
            target = str(holder.get("url") or "")
            if "meet.google.com" not in target:
                stop.wait(3)
                continue
            try:
                if not cdp_alive(companion_cdp):
                    companion_profile.mkdir(parents=True, exist_ok=True)
                    holder["companion_process"] = subprocess.Popen(
                        build_launch(
                            getattr(args, "browser_backend", "windows"),
                            companion_port,
                            companion_profile,
                            target,
                            browser=args.browser,
                            wsl_distro=getattr(args, "wsl_distro", None),
                            extra_args=["--mute-audio", "--new-window"],
                        ),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    if not told_sso:
                        told_sso = True
                        print("[companion] second browser opened -- sign in with the companion's Google account and JOIN the meeting yourself; I keep my hands off until you're in.")
                    deadline = time.time() + 60
                    while time.time() < deadline and not cdp_alive(companion_cdp) and not stop.is_set():
                        time.sleep(0.5)
                    companion_tab = None
                info = find_meet_tab(companion_cdp)
                if not info:
                    if operator_joined:
                        # The meeting moved after the operator was established:
                        # following it IS wanted automation.
                        open_url(companion_cdp, target)
                        companion_tab = None
                        holder["companion_tab"] = None
                        holder["companion_tab_id"] = None
                    elif not told_waiting:
                        told_waiting = True
                        print("[companion] waiting for YOU to open/join the meeting in the second window (no automation)...")
                    stop.wait(3)
                    continue
                if companion_tab is None:
                    companion_tab = CdpTab(info["webSocketDebuggerUrl"])
                    holder["companion_tab"] = companion_tab
                    holder["companion_tab_id"] = info.get("id")
                    mic_ready = False
                    reloaded_for_mic = False
                    mic_selected = False
                # Install the synthetic-mic patch ASAP so Meet's own
                # getUserMedia calls (prejoin preview, mic toggles) resolve
                # to our WebAudio destination instead of the real hardware.
                # Idempotent in JS (window.__sapiPatched guard).
                if not mic_ready:
                    try:
                        companion_tab.evaluate(GUM_PATCH_JS)
                        mic_ready = True
                    except Exception as error:  # noqa: BLE001
                        print(f"[companion] mic patch failed: {error}", file=sys.stderr)
                # Hands-off applies to SIGN-IN, not to joining: if the tab
                # shows a meet page with a Join/Rejoin button the SSO is
                # proven and the companion may (re)join unattended.
                state = str(companion_tab.evaluate(
                    "document.querySelector('button[aria-label*=\"eave call\" i]') ? 'in-call'"
                    " : location.hostname.includes('accounts.google') ? 'signin'"
                    " : [...document.querySelectorAll('button')].some(b => /join now|ask to join|join anyway|rejoin/i.test((b.textContent||'') + (b.getAttribute('aria-label')||''))) ? 'prejoin-ready'"
                    " : 'elsewhere'"))
                if state == "in-call" and not operator_joined:
                    operator_joined = True
                    log("[companion] you're in -- taking over: staying muted, deaf, and present.", role="companion")
                if state == "signin" and not operator_joined:
                    if not told_waiting:
                        told_waiting = True
                        print("[companion] waiting for YOU to sign in in the second window (no automation on sign-in pages)...")
                    stop.wait(4)
                    continue
                if state == "elsewhere":
                    # Signed in (this is meet.google.com, not a Google
                    # sign-in page) but not looking at our room -- e.g. still
                    # on the post-leave-call home screen. Safe to steer there
                    # regardless of operator_joined: no OAuth flow to disturb.
                    companion_tab.evaluate("location.href = %s" % json.dumps(target))
                    mic_ready = False
                    reloaded_for_mic = False
                    mic_selected = False
                    stop.wait(4)
                    continue
                if state == "prejoin-ready" and mic_ready and not reloaded_for_mic:
                    # Meet may have already grabbed a real-mic stream for its
                    # local preview before the patch landed. One reload here
                    # (still pre-join, so nothing live is disrupted) guarantees
                    # the very next getUserMedia call sees the patched version.
                    reloaded_for_mic = True
                    mic_ready = False
                    companion_tab.evaluate("location.reload()")
                    stop.wait(3)
                    continue
                if operator_joined and target.split("?")[0] not in str(info.get("url") or ""):
                    companion_tab.evaluate("location.href = %s" % json.dumps(target))
                    mic_ready = False
                    reloaded_for_mic = False
                    mic_selected = False
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
                # a mute click.
                if time.time() >= float(holder.get("speaking_until") or 0):
                    verdict = companion_tab.evaluate(autojoin_js("muted"))
                    if verdict in ("join-clicked", "stayed-in-call", "muted", "admitted"):
                        log(f"[companion] {verdict}", role="companion")
                # The companion is deaf as well as mute: silence every media
                # element so its tab never replays the meeting into the room
                # (the live mic would re-capture it as an echo). Always on.
                companion_tab.evaluate('document.querySelectorAll("audio,video").forEach((m) => { m.muted = true; m.volume = 0; })')
            except Exception as error:  # noqa: BLE001
                companion_tab = None
                holder["companion_tab"] = None
                holder["companion_tab_id"] = None
                log(f"[companion] {error}", err=True, role="companion")
                stop.wait(3)
            stop.wait(3)

    if args.companion:
        threading.Thread(target=companion_loop, daemon=True).start()
        print("[companion] armed: a muted second account will sit in the meeting so Google keeps it alive")

    def say_into_meeting(text: str) -> None:
        """/say <text>: SAPI-speak through the companion's synthetic mic.

        Never touches the real host mic -- this only works once --companion
        is running, has joined, and its getUserMedia patch has landed (or,
        with --tts-output-device configured, once Meet's mic dropdown is
        pointed at a virtual cable's recording side). Also doubles as the
        captioning self-test: speak known text through the companion and
        verify Google's own captions (Emit/Phrases/Transcribe) come back
        matching it.
        """
        tab = holder.get("companion_tab")
        if not tab:
            print("[say] no companion tab yet -- start with --companion and let it join first", file=sys.stderr)
            return
        with speech_lock:
            try:
                b64, duration = sapi_wav_base64(text)
                holder["speaking_until"] = time.time() + duration + 2.0
                unmute_verdict = tab.evaluate(autojoin_js("speaking"))
                time.sleep(0.3)  # let the UI settle before playing
                if tts_output_device_index is not None:
                    # Real virtual-cable path: play straight to the configured
                    # Windows device, bypassing the in-page WebAudio patch --
                    # Meet is already capturing from the cable's other side.
                    import base64 as _base64

                    play_wav_bytes_to_device(_base64.b64decode(b64), tts_output_device_index)
                    verdict = f"spoke-via-device-{tts_output_device_index}"
                    log(f"[say] {unmute_verdict}/{verdict}: {text[:80]}", role="companion")
                    stop.wait(duration + 0.2)
                else:
                    verdict = tab.evaluate(SPEAK_INTO_MEETING_JS % json.dumps(b64), await_promise=True, timeout=30)
                    log(f"[say] {unmute_verdict}/{verdict}: {text[:80]}", role="companion")
                    if isinstance(verdict, str) and verdict.startswith("speaking"):
                        stop.wait(duration + 0.2)
            except Exception as error:  # noqa: BLE001
                log(f"[say] failed: {error}", err=True, role="companion")
            finally:
                holder["speaking_until"] = 0.0
                try:
                    tab.evaluate(autojoin_js("muted"))
                except Exception as error:  # noqa: BLE001
                    log(f"[say] re-mute failed: {error}", err=True, role="companion")

    def switch_to(target_url: str | None) -> None:
        """Leave for another meeting: /join <url> or /new (fresh servant room)."""
        old_id = holder.get("tab_id")
        open_url(cdp_endpoint, target_url or "https://meet.google.com/new")
        room = re.compile(r"meet\.google\.com/[a-z]{3,4}-[a-z]{3,5}-[a-z]{3,4}(\?|$|/)", re.IGNORECASE)
        deadline = time.time() + 600
        info = None
        while time.time() < deadline and not stop.is_set():
            try:
                candidates = [entry for entry in list_tabs(cdp_endpoint)
                              if entry.get("type") == "page"
                              and "meet.google.com" in str(entry.get("url", ""))
                              and entry.get("id") != old_id]
                info = next((entry for entry in candidates if room.search(str(entry.get("url") or ""))), None)
            except Exception:
                info = None
            if info:
                break
            time.sleep(1.5)
        if not info:
            print("[bridge] switch failed: no new meeting tab appeared", file=sys.stderr)
            return
        old = holder.get("tab")
        holder["tab"] = CdpTab(info["webSocketDebuggerUrl"])
        holder["tab_id"] = info.get("id")
        holder["url"] = str(info.get("url") or "").split("?")[0]
        if old:
            try:
                old.close()
            except Exception:
                pass
        if old_id:
            # Hang up the previous meeting so we are not in two calls at once.
            close_tab(cdp_endpoint, old_id)
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
                        tab.bring_to_front()
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
                        companion_tab.bring_to_front()
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

    def sso_browsers(role: str | None = None) -> str:
        wanted = (role or "").strip().lower() or None
        if wanted == "guest":
            return "sso failed: guest/client tabs are not implemented yet"
        if wanted not in ("host", "companion"):
            return f"sso failed: unknown role {wanted!r}"
        target_url = "https://accounts.google.com/"
        if wanted == "host":
            tab = holder.get("tab")
            if tab is None:
                return "sso failed: host has no live tab"
            try:
                tab.evaluate(f"location.href = {json.dumps(target_url)}")
            except Exception as error:  # noqa: BLE001
                return f"sso failed: host navigation failed ({error})"
        else:
            tab = holder.get("companion_tab")
            if tab is None:
                return "sso failed: companion has no live tab"
            try:
                tab.evaluate(f"location.href = {json.dumps(target_url)}")
            except Exception as error:  # noqa: BLE001
                return f"sso failed: companion navigation failed ({error})"
        focus_verdict = foreground_browsers(wanted)
        verdict = f"sso:{wanted}"
        if "failed:" in focus_verdict:
            verdict += f" ({focus_verdict})"
        log(f"[bridge] {verdict}", role=wanted)
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
            holder[key] = process
            if wanted == "host":
                holder["tab"] = None
                holder["tab_id"] = None
                holder["url"] = None
            else:
                holder["companion_tab"] = None
                holder["companion_tab_id"] = None
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
                close_tab(companion_cdp, companion_tab_id)
                try:
                    companion_tab.close()
                except Exception:
                    pass
                holder["companion_tab"] = None
                holder["companion_tab_id"] = None
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
        <text>, /foreground [host|companion] (+ alias /focus), and
        /disconnect [host|companion] (+ alias /hangup); return a short
        verdict string if `command` was one of those and has been acted on,
        or None if it isn't a recognized control command. Shared by the
        mailbox-driven out_loop and the bridge's own HTTP /command endpoint
        (used by the ws_collab admin UI) so both paths behave identically.
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
                threading.Thread(target=say_into_meeting, args=(spoken,), daemon=True).start()
                return "speaking"
            return "say-empty"
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
        if lowered.startswith("/sso"):
            parts = command.split(None, 1)
            role = parts[1].strip() if len(parts) > 1 else None
            return sso_browsers(role)
        return None

    def out_loop() -> None:
        """ws_collab mailbox -> Meet chat (+ optional TTS), plus /join and
        /new commands."""
        while not stop.is_set():
            try:
                messages = mailbox.receive_new(args.outbox, limit=50)
            except Exception as error:  # noqa: BLE001
                print(f"[outbox] receive failed: {error}", file=sys.stderr)
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
                    print(f"[meet-chat] {verdict}: {line[:80]}")
                except Exception as error:  # noqa: BLE001
                    print(f"[meet-chat] failed: {error}", file=sys.stderr)
                if args.speak:
                    threading.Thread(target=speak_windows, args=(text,), daemon=True).start()
            stop.wait(1.5)

    if not args.no_out:
        threading.Thread(target=out_loop, daemon=True).start()
        tts_note = " + TTS" if args.speak else ""
        print(f"[bridge] OUT armed: mailbox '{args.outbox}' -> Meet chat{tts_note} (commands: /join <url>, /new, /say <text>)")
    print(f"[bridge] IN armed: captions -> mailbox {recipients}")
    if not args.no_autojoin:
        print("[bridge] unattended: I click Join, keep the mic ON, and turn captions on myself.")

    warned = ""
    autojoin_at = 0.0
    last_autojoin_verdict = ""
    lost_since: float | None = None
    fallback_logged_keys: set[str] = set()
    try:
        while True:
            tab = holder["tab"]
            try:
                raw = tab.evaluate(CAPTIONS_JS)
                payload = json.loads(raw) if isinstance(raw, str) else {"ok": False, "note": "no payload"}
                lost_since = None
            except Exception as error:  # noqa: BLE001
                print(f"[bridge] tab lost ({error}); reattaching?", file=sys.stderr)
                time.sleep(2.0)
                info = find_meet_tab(cdp_endpoint)
                if info:
                    try:
                        tab.close()
                    except Exception:
                        pass
                    holder["tab"] = CdpTab(info["webSocketDebuggerUrl"])
                    lost_since = None
                elif not args.attach_only:
                    lost_since = lost_since or time.time()
                    if time.time() - lost_since > 20:
                        print("[bridge] meeting gone -- creating a fresh servant meeting...")
                        lost_since = None
                        switch_to(None)
                continue
            if payload.get("ok"):
                # Log once PER ROW KEY (not every poll -- a still-growing row
                # would otherwise spam this every ~0.4s) when the speaker/
                # text split heuristic didn't cleanly apply for that row.
                for r in payload.get("rows") or []:
                    if r.get("speaker") == "Speaker" and r.get("key") not in fallback_logged_keys:
                        fallback_logged_keys.add(r["key"])
                        log(f"[captions] speaker-split fallback used: {r.get('text', '')[:100]!r}", role="host")
                tracker.update(payload.get("rows") or [], payload.get("liveKeys") or [], emit)
                note = payload.get("note") or ""
            else:
                note = payload.get("note") or "captions not found"
            if note and note != warned:
                warned = note
                print(f"[bridge] {note}")
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
            time.sleep(args.poll)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        try:
            holder["tab"].close()
        except Exception:
            pass
        print("[bridge] stopped")


if __name__ == "__main__":
    main()
