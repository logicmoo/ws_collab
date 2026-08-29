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
  server on `127.0.0.1:8802`. GET requests against them (`/health`,
  `/captions`) are fine and expected. Never POST an untested/mutating
  `/command` to the live bridge. Never `Stop-Process`/kill either of those
  processes unless the user explicitly asks you to restart them — if you
  do restart the bridge, use the same launch args it was last using
  (check `Get-CimInstance Win32_Process` for the live command line first).
- Don't touch `CURRENT_AGENTS.md` / `CURRENT_AGENTS.tmp.md`.
- Full pytest suite must pass before every commit. Syntax-check
  (`node --check` for `.js`, `python -m py_compile` for `.py`) every file
  you touch before running tests.
- Windows paths, backslashes, PowerShell (no `&&`/`||`, use `;` and
  `if ($?) { ... }`).

## What this whole thread has been building

`ws_collab` runs a Google Meet caption bridge as a **separate OS process**
(`src\ws_collab\meet_bridge\bridge.py`, console script
`ws-collab-meet-bridge`) that HTTP-serves `/health` + `/captions` +
`/command` on port 48699. The main FastAPI admin server (`src\ws_collab\
service.py` + `rest.py` + `admin\{index.html,app.js,app.css}`) polls that
bridge and renders a rich "Google Meet" ops page (`#meet`) plus a merged
"SSO / Browser" settings page (`#browser`). The two processes are
independent — the admin server has never spawned/controlled the bridge.

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
10. Meeting-cell copy-link — the Connectors table's "Meeting" column shows
    just the room id (e.g. `bgb-xqts-xjt`) and copies the full URL to the
    clipboard on click (`meetCopyLink()`), instead of showing/linking the
    raw URL. The separate meeting-URL link in each `<details>`'s
    `<summary>` (opens the meeting in a new tab) is untouched.
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
      this named secondary source: `POST /v1/audio/secondary-capture/start`
      (body `{device_id}`), `POST /v1/audio/secondary-capture/stop`, plus a
      way to read its state.
    - `bridge.py`'s `companion_loop()`: tab-muting is now conditional on a
      new `--companion-listen-device <name>` flag (parallel naming to
      `--tts-output-device`/`--mic-select-device`). When set, the
      companion's `<audio>/<video>` elements are **not** muted/deafened
      (so their audio actually plays out to whatever real device the
      operator has routed that Chrome window's output to via Windows' own
      per-app audio mixer — a manual, one-time setup step, same category as
      the existing virtual-cable setup) and it logs clearly that this is
      intentional. Default (flag unset): today's exact mute/deafen
      behavior, unchanged.
    - Devices-page UI controls/state for the new secondary capture source.
    - This is genuinely not end-to-end testable without real hardware/a
      live Meet call — only unit-testable pieces (start/stop/state
      transitions with a mock device backend, REST wiring, the bridge's
      conditional-mute logic) have real test coverage.

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
