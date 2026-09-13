# HANDOFF: ws_collab Google Meet bridge / admin UI work

Read this whole file before making any changes. It is the authoritative TODO
list and status record for this thread of work — it supersedes any other
notes. Repo: `logicmoo/ws_collab`, branch `master`. Last commit as of this
handoff: `23c02d2` (pushed, no divergence from `origin/master`). All 280
tests pass at this commit (`pytest --basetemp=.pytest_tmp -q`, with
`$env:TMP`/`$env:TEMP` pointed at a fresh writable dir first if you hit a
`PermissionError` on the shared machine's default temp dir — a known
pre-existing environment quirk, unrelated to this code).

## Scope discipline (read this first)

- Only touch files inside this `ws_collab` repo
  (`C:\snet\PeTTa\repos\symbolic_learner_workbench\workbench\plugins\ws_collab`).
  Never touch the outer `symbolic_learner_workbench` monorepo.
- There may be a REAL, LIVE Meet bridge process running on
  `127.0.0.1:48699` (the actual, in-use Google Meet call) and a real admin
  server on `127.0.0.1:8802`. The bridge's canonical loopback reads are
  `/ws_collab/meet-bridge/health` and
  `/ws_collab/meet-bridge/captions`. Never POST an untested/mutating
  `/ws_collab/meet-bridge/command` to the live bridge. Never
  `Stop-Process`/kill either of those
  processes unless the user explicitly asks you to restart them — if you
  do restart the bridge, use the same launch args it was last using
  (check `Get-CimInstance Win32_Process` for the live command line first).
- Don't touch `CURRENT_AGENTS.md` / `CURRENT_AGENTS.tmp.md`.
- Updated operator authorization (2026-09-12): restart WS_COLLAB on port 8802
  and refresh its captioner automatically whenever a change is ready, without
  asking again. Use graceful shutdown plus a fresh interpreter when loading
  changed Python modules. This does not authorize restarting the Meet bridge.
- During active work, periodically read recent user voice/chat input for requests
  at meaningful checkpoints. Distinguish user requests from model output/echo.
  Do not create an external agent keep-alive loop for this.
- Full pytest suite must pass before every commit. Syntax-check
  (`node --check` for `.js`, `python -m py_compile` for `.py`) every file
  you touch before running tests.
- Windows paths, backslashes, PowerShell (no `&&`/`||`, use `;` and
  `if ($?) { ... }`).

## What this whole thread has been building

`ws_collab` runs a Google Meet caption bridge as a **separate OS process**
(`src\ws_collab\meet_bridge\bridge.py`, console script
`ws-collab-meet-bridge`) that HTTP-serves the internal
`/ws_collab/meet-bridge/{health,captions,command}` namespace on port 48699.
The main FastAPI admin server (`src\ws_collab\
service.py` + `rest.py` + `admin\{index.html,app.js,app.css}`) polls that
bridge and renders a rich "Google Meet" ops page (`#meet`) plus a merged
"SSO / Browser" settings page (`#browser`). The two processes are
independent — the admin server has never spawned/controlled the bridge.

Current boundary: Meet captions use
`POST /ws_collab/meet/captions/ingest` as durable conversation/chat context.
They are not an STT driver and do not emit `HEARD_SPEECH`. Always-on microphone
captioning belongs to the supervised `/ws_collab/captioner/` Chrome Web Speech
page; Chrome may send microphone audio to Google/cloud and it is not
offline/local recognition. Its `chrome_captioner` user-data directory and CDP
port are dedicated and never reuse/import the Meet SSO profile, cookies, account
registry, or Google auth flow. The canonical admin SPA is
`/ws_collab/#chrome-captions` (`/ws_collab/admin/` is an alias). Its source
actions use the backend's durable generic `browser_captioner` / `google_meet`
policy: one enabled source is primary, Chrome primary suppresses only safe
recent Meet duplicates, and Meet primary suppresses Chrome canonical
publication while acknowledging its queue and retaining bounded diagnostic
final state. `Disable other STT(s)` is independent. Neither source Disable
stops the Meet bridge/profile nor removes history/configuration.

Every captioner tab registers its stable browser-session ID plus a per-load
instance ID. Backend automatic/pinned selection is authoritative; only the
selected, enabled, fresh instance may acquire microphone/Web Speech/VAD.
Unselected, disabled, and stale transcript/floor requests are explicitly
suppressed or rejected. Per-instance Disable is boot/tab-instance scoped and
operator pins fail over only after the stale timeout.

Browser RMS VAD now also sends metadata-only, ordered transitions through the
loopback/same-origin captioner channel. The server treats only
`browser_rms_vad` + `local_microphone` + `input_scope: microphone` as the local
mic floor: onset blocks/cancels conversational TTS and backchannels, end uses a
350ms hangover, and stale input becomes unknown/fail-open. Every conversational
playback path re-checks immediately before audio; floor release never speaks.
The caption page requests ideal echo/noise suppression and reports bounded
actual track settings without raw device IDs. It cannot directly hear another
tab or source-separate speaker leakage. Chrome desktop 135+ now receives the same
live microphone track for Web Speech and RMS detection via `start(audioTrack)`;
there is no silent fallback to a different default recognition input.

Caption lists now show completed silence durations and a live trailing silence
counter, separate from "Last caption ... ago". "Paused" means an explicit user
listening pause, never a quiet interval or backend standby. Normal Web Speech
restarts keep local RMS detection running; capture interruption invalidates its
silence timing. Caption toolbar labels/tooltips explain Open captioner, Show
window, Pause listening, and Resume listening.
Background silence sampling now uses an AudioWorklet and the audio sample clock,
not throttled page timers. Caption lists show speech-in-progress before final
recognition, including provisional words and inline silence markers. Internal
VAD event fields are stripped before strict envelope validation.

ChatBot Test (`/ws_collab/#chatbot-test`) configures a registered conversational
agent backed by the existing emullm API at `http://127.0.0.1:8801/v1`, using
`emullm/default` unless the operator selects another model. Its prompt and
submitted history are per-agent. When enabled it consumes the shared finalized,
non-echo STT pipeline, not a second microphone. Spoken replies use the owning
browser's real speech-synthesis voice; the server's fake TTS test driver is not
used for these replies. Configuration/history persist, but a new server boot
starts chat stopped. No emullm source files or service configuration were changed.
ChatBot Test now uses half-duplex input gating: model generation and all queued
speech must finish before user input is accepted again; STT/VAD itself continues.
Actual browser speech completion, not estimated duration, opens the gate.
Per-turn timing traces separate STT delivery, turn wait, model dispatch/first
token/completion, TTS queue/playback, and total observed latency.
Workers such as Copilot can speak through `/ws_collab/language-chat/agent-speech`
using the same monitored output queue, without an LLM call or recycled user input.
Model/endpoint fields allow free-text drafts even while active or disconnected;
Stop then Save applies them. Top-bar and System & Audit restart/shutdown controls
use the guarded lifecycle routes. `python -m ws_collab.standalone
start|status|restart|shutdown` provides external controls and readiness checks.
The local `plugin.start_server()` API can start a stopped standalone service;
there is no HTTP start route inside the stopped server. Foreground supervision
uses fresh child interpreters on restart, with repo-root `collab_state` by default.
Connection draining is bounded to ten seconds so disconnected browser transports
cannot hold shutdown open indefinitely. Deployment note (2026-09-12): another
checkout occupied port 8802; it was left untouched. This checkout was started and
restarted on 8803 with its existing state and `gpt-5.6-sol` selection preserved.
Use `--port 8803` for its CLI controls and `/ws_collab/#chatbot-test` on that port.

The two-bot design: a HOST identity (real hardware mic/speakers, never
automated) and a COMPANION identity (a second signed-in Google account,
muted+deaf, just to keep Meet from ending a single-participant call) sit in
the same meeting. `--companion` arms it. `/say <text>` speaks synthetic TTS
through the companion's mic — this doubles as a live captioning self-test.

## STATUS: everything below this line is DONE and pushed, at commit `23c02d2`

(Full list preserved for context/history — skip to "REMAINING WORK" below if
you just want what's left.)

1. Role-tagged debug logging — `bridge.py`'s `log(text, role=...)`
   ("host"/"companion"/"bridge"), admin debug table has a Source column.
2. Per-connector Foreground/Disconnect actions — `foreground_browsers(role)`
   / `disconnect_browsers(role)` in `bridge.py`, `/foreground` `/disconnect`
   commands (role-scoped host/companion/guest, "guest" always honestly
   returns "not implemented yet" rather than silently no-op'ing), per-row
   buttons in `meetUsRows()` in `app.js`.
3. SSO/profile-dir display + live per-room snapshot — `_host_profile_info()`,
   per-client `.profile`, `meeting_state` dict keyed by room id (e.g.
   `"bgb-xqts-xjt"`) exposed as `health.meetingState`, used to show
   "last known" state for a not-current meeting instead of blank dashes.
4. Client meeting kind (GUEST_CLIENT) — `DEFAULT_CLIENT_MEETING_URLS` in
   `app.js` (currently contains `https://meet.google.com/qmj-bkbk-mik` as a
   configured, never-auto-joined placeholder), single GUEST_CLIENT
   Connectors row, "(COMPUTER)"/"(AVAILABLE)" wording (was "(PHYSICAL
   COMPUTER)"/"(past)"), HOST's Mic/Speak cells now say "COMPUTER" and link
   to `#devices` instead of hardcoded text.
5. `<details>` open/closed state preserved across `renderMeetTree()`
   rebuilds; gentle 3s auto-polling (`loadMeetWithPolling()`) that skips a
   tick if the operator is actively typing in an input on that page.
6. Selectable browser backend — `--browser-backend windows|wsl` in
   `bridge.py`/`cdp.py` (`build_launch()` is the one shared argv builder for
   both host and companion, both backends). `wsl` backend runs Chrome
   inside WSL2 under a real Xvfb virtual display (genuinely invisible on
   the Windows desktop, not just off-screen-positioned) — reachable via
   WSL2's automatic `127.0.0.1` port forwarding. `windows` is unchanged
   default. `/foreground` on a `wsl`-backend identity honestly reports "no
   OS window to foreground by design" instead of silently doing nothing.
   `health.browserBackend` surfaces which one is active.
7. Processes page (`#processes`) — tracks the actual `subprocess.Popen`
   handles for host+companion (`holder["host_process"]`/`["companion_
   process"]`), shown via `health.processes` (pid/alive/port/profile/
   backend), with Foreground + a new `/kill-process <role>` command
   (distinct from `/disconnect`, which only closes a CDP tab — this
   actually terminates the underlying browser process tree).
8. SSO manager — originally its own `#sso` page, **now merged into
   `#browser`** (see item 12). Lets an operator launch/refresh a Google
   sign-in for a profile, or wipe it, without the bridge needing to be
   running. Prefers reusing the bridge's own already-open window (new
   `/sso <role>` bridge command: navigates the existing tab to
   `accounts.google.com` + foregrounds it) over spawning a redundant
   second Chrome process against the same profile dir (which risks
   corrupting Chrome's profile lock) — only falls back to a standalone
   launch when the bridge isn't running or that identity has no live tab.
9. Row-count presets — Emit/Phrases/Transcribe toolbars now show
   `[3] [10] [20] [ALL]` preset buttons + an "Exact" custom textbox
   (replacing the old plain `<input type="number">`), `"all"` is a valid
   stored value meaning "no height cap, grow with content."
10. Meeting URL/copy is meeting-scoped — the redundant Connectors-table
    `Meeting` column was removed. The `<details>` header retains the URL link
    and now has a `Copy meeting` action.
11. Default Chrome profile directory moved from
    `~/.cache/ws_collab_models/meet_bridge_profile` to
    `<plugin_root>/collab_state/meet_bridge_profile` (`cdp.py`
    `DEFAULT_PROFILE`), with automatic one-time migration (copies the old
    directory's contents to the new location the first time the new
    default path doesn't exist yet, logs when it does so, never deletes
    the old copy). `WS_COLLAB_MEET_PROFILE_DIR` env override unchanged.
12. **Global toolbar consolidation** on `#meet`: one box combining a
    unified "MEETS" list (Driver and Client meetings are no longer
    separate top-level sections — one list, each item still badged
    DRIVER/CLIENT) with a persisted `Show: Driver Client` kind filter
    (checkboxes, default both on), a global "Clear all" button, a global
    "Exact" row-count input, and a global "Autoscroll" toggle — all four
    apply across Emit/Phrases/Transcribe simultaneously, on top of (not
    replacing) each section's own individual controls.
13. **Shared Chrome profile mode (opt-in)** — `bridge.py`:
    `--profile-mode {separate,shared}` (default **`separate`**, i.e. today's
    original two-profile/two-process design, completely unchanged unless
    an operator explicitly passes `--profile-mode shared`).
    - In `shared` mode: ONE Chrome profile/process. HOST and COMPANION (and
      a reserved future GUEST slot) are each just a **tab** in that one
      process, addressed via Google's own `?authuser=N` URL parameter
      (`with_authuser()`/`authuser_from_url()` helpers). Default slot
      assignment: `_default_authuser_for_role()`; overridable per-role via
      repeatable `--role-authuser ROLE=N` (e.g.
      `--role-authuser host=0 --role-authuser companion=1`).
    - `service.py` / `meet_browser_settings.py`: the SSO model is now
      **account-centric**, not role-centric, when in shared mode — a flat
      list of signed-in Google accounts, each with a stable local ID
      (`sso_1`, `sso_2`, ...) assigned in first-discovered order
      (`_sso_sort_key()`), NOT the raw Google `authuser` index (which could
      shift). A separate, independently-persisted `role_account_map`
      (`{"host": "sso_1", "companion": "sso_2"}`) maps roles to accounts.
      Both are durably persisted via `MeetBrowserSettings.
      get_shared_profile_state()`/`set_shared_profile_state()` (atomic
      JSON store at `collab_state/meet_browser_settings.json`, same
      pattern as `sound_settings.py`). Live `authuser`+email detection
      (via a `whoami(tab)`-style JS eval reading
      `a[aria-label*="Google Account"]`'s aria-label) **reconciles/extends**
      this stored state when it succeeds, but a previously-learned email is
      still shown even when a live check currently isn't possible (bridge
      not running, tab not on a Google-branded page, etc.) — it is never
      forgotten just because it can't be reconfirmed right now.
    - `admin\index.html`/`app.js`: the standalone `#sso` nav page/item is
      **gone** — merged into `#browser` ("SSO / Browser") as two clearly
      separated sections on one page: a "Browser" section (profile path,
      `--browser-backend` windows/wsl, `--profile-mode` separate/shared,
      generated next-launch CLI command preview with a copy button) and an
      "SSO" section (flat account list with a "sign in another account"
      action, role→account assignment dropdowns). Every place that used to
      link to `#sso` (the Connectors table's SSO column, etc.) now links to
      `#browser` instead. The SSO column's displayed text now prefers the
      detected Google email over the raw profile path (falls back to path
      if no email is known yet) — the `<a href="#browser">` wrapper itself
      is unchanged, only the label text changed.
14. **Companion incoming-audio → own STT tap** (this was the "big greenlit
    task") — built as a genuinely **additive** second capture pipeline that
    does **not** modify `src\ws_collab\audio\capture.py`'s existing
    single-source `CaptureService` at all:
    - New `src\ws_collab\audio\secondary_capture.py` (or similarly named —
      verify exact filename) reuses the same VAD/segment primitives
      (`vad.py`/`segment.py`) and feeds completed segments into the same
      `run_stt()` path everything else uses, tagged with a distinct
      `source_id` (e.g. `"meet-companion-incoming"`).
    - New REST pair mirroring the primary capture's shape but scoped to
      this named secondary source:
      `POST /ws_collab/audio/secondary-capture/start`
      (body `{device_id}`), `POST /ws_collab/audio/secondary-capture/stop`, plus a
      way to read its state.
    - `bridge.py` keeps legacy direct remote-MediaStream capture available,
      while validated two-cable mode routes RECEIVE into the server's
      secondary capture and TRANSMIT into the companion. Browser media stays
      fail-closed until exact sink/track/capture verification.
    - Devices-page UI controls/state for the new secondary capture source.
    - This is genuinely not end-to-end testable without real hardware/a
      live Meet call — only unit-testable pieces (start/stop/state
      transitions with a mock device backend, REST wiring, the bridge's
      conditional-mute logic) have real test coverage.
15. **Per-meeting routing and lifecycle policy** — the active browser profile's
    atomic `MeetBrowserSettings` record now stores room-adapter kind/id,
    HOST/COMPANION mic and speaker descriptors, optional companion wiring,
    one default-on-explicit-bridge-start policy, and reconnect policy. Legacy
    stored `autostart` values are preserved as that default and never bootstrap
    the bridge. Only the physical-computer
    adapter is implemented; future kinds are explicit unavailable capability
    records. The meeting header edits policy, connector rows edit devices and
    apply them with verified `Sync devices`, and role-scoped mute buttons remain
    separate manual overrides. Explicit Disconnect suppresses bounded reconnect
    until Join/Rejoin/Sync/new bridge start.

## REMAINING WORK — this is the actual TODO, do these in order

### 1. Flip `--profile-mode` default from `separate` to `shared` (highest priority — explicit, repeated user instruction)

The user's final, explicit direction (after initially asking for shared
mode to be opt-in, then reversing that) is: **`shared` becomes the
default**; `separate` (today's original two-profile/two-process design)
becomes the thing an operator has to explicitly opt INTO via
`--profile-mode separate` if they want the old behavior (e.g. for
troubleshooting/rollback). This has **NOT** been done yet — verify by
checking:
- `src\ws_collab\meet_bridge\bridge.py`: the `argparse.add_argument(
  "--profile-mode", choices=["shared", "separate"], default="separate",
  ...)` line — change `default="separate"` to `default="shared"`.
- `src\ws_collab\meet_browser_settings.py`: `MeetBrowserSettings.
  get_profile_mode()`'s fallback (`self.get(self._PROFILE_MODE_KEY,
  "separate")` and the "not in {...}" fallback) — change the fallback
  default to `"shared"` too, so a fresh install (no persisted setting yet)
  also defaults to shared, consistently with the CLI default.
- Update any place in `app.js`/`index.html` that shows/assumes a default
  value for the profile-mode dropdown/selector on the "SSO / Browser" page,
  so it reflects `shared` as the pre-selected default for a fresh
  install too.
- Update `docs/GOOGLE_MEET_BRIDGE.md` to describe `shared` as the default
  mode and explain what changes about it (one profile, multiple signed-in
  accounts, `?authuser=N` tabs) vs. `separate` as the legacy/opt-out mode.

**Important**: the actual, currently-live/running bridge process was
already launched using the OLD default (`separate` mode, two profile
dirs) before any of this work started. Flipping the code default does
**not** retroactively change that already-running process — it only
affects the *next* time `ws-collab-meet-bridge` is launched (a manual
restart, which you should NOT do yourself unless explicitly asked to).
Make this limitation clear in your final report/commit message.

### 2. Two real accounts to design/test against

The user specified the two real Google accounts that will actually be
signed into the one shared profile once an operator switches over:
- `logicmoo@gmail.com`
- `pharaohcorp@gmail.com`

Which one is HOST vs. COMPANION is the **operator's choice** via the
role-assignment mapping (`role_account_map`) — do not hardcode either
account to a specific role in code. If you get to a point where you can
safely verify the shared-profile flow end-to-end (e.g. the operator
explicitly asks you to test it, or a throwaway/disposable test profile
scenario is set up), these are the two accounts that would actually be
used — but do not attempt any live sign-in flow against the real bridge
profile without being explicitly asked to, since that's an operator-driven
manual step (Google account picker), not something to script.

### 3. Verify the whole shared-profile + account-model feature holds together end to end (by inspection/unit test, not live)

Re-read `service.py`'s new account-centric SSO methods
(`list_meet_sso_profiles`, `open_meet_sso_profile`,
`forget_meet_sso_profile`, `_shared_account_summary`,
`_shared_sso_target`, `_meet_sso_in_use_warning`) end to end against the
spec above and confirm:
- `sso_N` IDs are genuinely stable across restarts (re-detection should
  reuse an existing `sso_N` for a known email/authuser pair, not mint a
  new one every time).
- The `role_account_map` correctly rejects/reports a conflict if two roles
  are assigned to the same `sso_N` account (Google would treat that as the
  same account joining twice — a duplicate-tab situation, not two
  participants) — check whether this validation already exists (grep for
  "duplicate" or similar near `role_account_map` handling) and add it if
  it's missing.
- The "SSO / Browser" page's two sections (Browser vs. SSO) are visually
  and functionally distinct, not interleaved, per the user's explicit
  layout request.

### 4. Once 1–3 are done: full verification pass

- Syntax-check every touched file (`node --check`, `py_compile`).
- Full `pytest` suite (expect **280+** passing — do not regress this
  number without a clear, justified reason).
- Browser-check (`http://127.0.0.1:8802/ws_collab/admin/#browser` and
  `#meet`) with a real cache-busting reload (append `?t=<timestamp>` or use
  your browser tool's hard-reload/disable-cache option — the *server*
  always serves fresh `app.js` per request via `FileResponse`, any
  staleness you see is your own browser tool's HTTP cache, not a server
  issue, don't misdiagnose this a second time).
- Commit with a clear message + this trailer:
  ```
  Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
  ```
- `git fetch origin master` first to confirm no divergence before
  `git push` (this repo has stayed a clean fast-forward the whole session
  — if it's somehow diverged, STOP and report instead of force-pushing).

## Key files quick-reference

- `src\ws_collab\meet_bridge\bridge.py` — bridge CLI/orchestration/HTTP
  server. `--profile-mode` at ~line 224, `_default_authuser_for_role`/
  `parse_role_authusers`/`authuser_from_url`/`with_authuser` near the top,
  `companion_loop()` for the shared-vs-separate companion launch logic.
- `src\ws_collab\meet_bridge\cdp.py` — `build_launch()` (shared argv
  builder, both backends), `DEFAULT_PROFILE`, `companion_profile_path()`.
- `src\ws_collab\meet_browser_settings.py` — `MeetBrowserSettings`,
  `get_profile_mode`/`set_profile_mode`, `get_shared_profile_state`/
  `set_shared_profile_state`.
- `src\ws_collab\service.py` — `_sso_sort_key`, `list_meet_sso_profiles`,
  `open_meet_sso_profile`, `forget_meet_sso_profile`,
  `_shared_account_summary`, `_shared_sso_target`, `_meet_sso_profile_path`,
  `_meet_sso_in_use_warning`, `_voice_profiles_with_activity` (unrelated,
  don't confuse with SSO).
- `src\ws_collab\rest.py` — `meet_sso_profiles`/`meet_sso_open`/
  `meet_sso_forget` route handlers (~line 779-813), `meet_browser_settings`
  routes (grep for `browser-settings`).
- `src\ws_collab\admin\app.js` — `loadBrowserSettings()` (~line 1671),
  `renderMeetTree()`, `meetUsRows()`, `meetCopyLink()`,
  `DEFAULT_CLIENT_MEETING_URLS` (~line 61), `postMeetSso()`.
- `src\ws_collab\admin\index.html` — `data-page="browser"` nav item
  (~line 112) and its page section.
- `src\ws_collab\audio\capture.py` — the ORIGINAL, still-untouched
  single-source `CaptureService` — do not modify for the companion-tap
  feature; the new secondary capture module is separate.
- `docs\GOOGLE_MEET_BRIDGE.md` — bridge design doc, update per item 1 above.

## Environment notes carried over from this session

- pytest tmp-dir: the shared machine's default temp dir
  (`C:\Users\dougl\AppData\Local\Temp\pytest-of-dougl`) can be
  inaccessible (`Access is denied`) — pre-existing, unrelated to any code
  change. Workaround:
  ```powershell
  New-Item -ItemType Directory -Force -Path .pytest_tmp | Out-Null
  $env:TMP = (Resolve-Path .pytest_tmp).Path; $env:TEMP = $env:TMP
  python -m pytest --basetemp=.pytest_tmp -q
  ```
  `.pytest_tmp/` is now gitignored — don't commit it, delete it when done.
- PowerShell + `git diff > file.txt` defaults to UTF-16LE, which renders
  garbled in plain-text viewers. Use
  `git diff ... | Out-File -FilePath file.txt -Encoding utf8` instead.
- The live bridge, if running, was last confirmed healthy as
  `service: "ws_collab_meet_bridge"` (the NEW native bridge, already cut
  over from the old outer-repo script) on `meetingUrl:
  "https://meet.google.com/bgb-xqts-xjt"`, `--companion --tts-output-device
  17 --mic-select-device "CABLE Output"`, in `separate` profile mode
  (today's default at the time it was launched).
