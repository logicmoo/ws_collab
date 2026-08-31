# Google Meet bridge

`ws_collab.meet_bridge` uses **Google Meet's own live captions** as a speech
recognizer -- far better than a local STT model for a room with real
participants. It drives a real Chrome tab over the DevTools Protocol (CDP; no
Playwright/Selenium), and has two jobs:

1. **Servant meeting (always-on STT)** -- run with no arguments and it
   creates an instant meeting, joins it unattended (room mic ON, camera OFF,
   captions ON), and transcribes whatever the microphone hears. You never
   need to join this meeting; it is simply the recognizer. The bridge answers
   Google's "are you still there?" prompts, auto-admits knockers, and when
   Google ends the meeting it creates a fresh one.
2. **Invited meetings (talk with it together)** -- point the bridge at any
   meeting via `/join <url>` (mailbox command, or the admin UI's Meet Bridge /
   Google Meet pages). Everyone talks normally; every finished caption line
   is posted into the mailbox as `meet-<speaker>`, and anything sent to the
   `google-meet` mailbox is typed into the Meet's in-call chat for everyone to
   see (`--speak` also voices it locally with Windows TTS).

## Why two bots (HOST + COMPANION)

Google ends or nags a meeting that has only one silent participant. `--companion`
keeps a **second** signed-in Google account sitting **muted and deaf** in the
call so Google always sees two participants. Both accounts use one Chrome
profile and process, with one tab per account selected by Google's
`?authuser=N` URL parameter. The real (HOST) account's microphone is never
touched by automation; the companion's "microphone" is a synthetic in-page
WebAudio source (or, if `--mic-select-device`/`--tts-output-device` are
configured, a real virtual-cable device) that only carries `/say` speech,
never the room.

Tab uniqueness is scoped by the composite `(connector, SSO identity)`, never by
browser-wide URL similarity. Reopening an account, restarting the bridge, or
moving a role to another meeting focuses and, when needed, navigates that
scope's existing tab instead of opening a duplicate. Tabs belonging to another
connector or another SSO role/account remain intentionally separate.

## Running it

```
pip install -e ".[meet,audio]"          # websocket-client, scipy + sounddevice/numpy
ws-collab-meet-bridge                    # ALWAYS-ON servant meeting
ws-collab-meet-bridge --meet <meet-url>  # join a given meeting
ws-collab-meet-bridge --companion        # + a second muted account
ws-collab-meet-bridge --list-audio-devices
```

First run: one Chrome window pops up with a persistent profile. Sign in every
Google account the bridge will use; the sessions persist across runs.
`--forget-sso` wipes the whole browser profile.

Every driver launch has a live SSO preflight. The browser opens to Google's
account chooser, but no Meet tab or HOST/COMPANION automation starts until two
distinct accounts report active signed-in sessions in the required `authuser`
slots. Persisted `sso_N` records alone do not satisfy this gate.

## Accounts and Meet roles

Browser sign-in state is account-centric. The admin **SSO / Browser** page
discovers accounts in the profile and gives them stable local IDs (`sso_1`,
`sso_2`, ...). Those IDs remain attached to known accounts even if Google
changes their numeric `authuser` slots. `role_account_map` is only the mapping
from a Meet role such as HOST or COMPANION to one of those account IDs. Each
role is assigned independently; multiple roles may select the same account.

The next-launch command includes the current numeric slots:

```
ws-collab-meet-bridge --companion \
  --role-authuser host=0 \
  --role-email host=first.account@example.test \
  --role-authuser companion=1 \
  --role-email companion=second.account@example.test
```

There are no implicit role defaults. Assign the live accounts on the **Google
Meet** page; its launch arguments translate the stable `sso_N` choices into
Google's current numeric `authuser` slots and include the expected email. The
bridge verifies each live slot/email pair and each created Meet page before it
allows join automation.

The admin UI stores role accounts as global defaults. Every meeting without its
own configuration inherits those defaults. Use **Accounts** on a meeting to save
an independent HOST/COMPANION override, or **Use global defaults** to remove the
override and resume inheritance.

Each meeting's Connector table also exposes the assignment directly as a combo.
It lists every known identity as `email-or-name (sso_N)`. Choosing `(default)`
removes that role's meeting override; the stable `sso_N` is translated to the
current email and numeric `authuser` slot only when the bridge command is built.

The Browser and SSO panels are intentionally account-only: Browser configures
the single profile path and backend, while SSO manages signed-in accounts.
HOST/COMPANION assignment is shown only on the Google Meet page because it is
meeting orchestration, not browser state. These settings affect only the next
bridge launch; they do not reconfigure a running process.

## Browser backend

By default the bridge launches a normal visible **Windows** Chrome/Edge window.
You can keep that unchanged, or switch the browser hosting backend:

```
ws-collab-meet-bridge --browser-backend windows   # default
ws-collab-meet-bridge --browser-backend wsl        # run Chrome inside WSL2 + Xvfb
ws-collab-meet-bridge --browser-backend wsl --wsl-distro Ubuntu-24.04
```

- `--browser-backend windows` keeps today's behavior: native Windows browser
  window, visible on the desktop and foreground-able with `/foreground`.
- `--browser-backend wsl` launches Chrome/Chromium **inside WSL2** under a real
  `Xvfb` virtual display, so there is **no Windows OS window at all**. CDP is
  still reached from the Windows-side Python bridge at `http://127.0.0.1:<port>`.
- `--wsl-distro` picks which distro to use; otherwise the first distro from
  `wsl -l -q` is used.

WSL mode expects Chrome/Chromium and `Xvfb` to already be installed inside that
distro. Modern WSL2 usually forwards the CDP port to Windows loopback
automatically. If it does not on your machine, try:

```ini
[wsl2]
networkingMode=mirrored
```

in `%USERPROFILE%\.wslconfig`, then run `wsl --shutdown` and start the bridge
again.

Limitation: in `--browser-backend wsl` mode, `/foreground` can only report that
the tab exists -- it cannot raise a real Windows OS window, because Xvfb-hosted
Chrome has no desktop window by design.

## Server-managed HTTP API

The ws_collab server owns the bridge worker lifecycle and exposes its API through
the authenticated `/ws_collab/v1/meet/bridge` routes:

- `GET /status` -- `{ok, service, meetingUrl, captionCount, lastCaptionAt,
  outbox, recipients, clients, debug}`.
- `GET /captions?since=<epoch>` -- every caption row (`key`, `at`,
  `updated_at`, `speaker`, `text`, `final`, `replaces`, `meetingUrl`) whose
  `updated_at` is newer than `since`.
- `POST /command {"command": "/join <url>" | "/new" | "/say <text>"}`.
  Join and New start the worker when it is offline.
- `POST /speech` is the structured virtual-agent route. It requires
  `destination: "companion"` plus text and accepts meeting, utterance, agent,
  voice, correlation, and artifact-source metadata. It returns HTTP 409 rather
  than claiming success if the companion/tab/meeting/synthetic mic is not ready.
- `POST /speech/cancel` cancels a queued or active utterance by ID.

Agent speech and automatic `uh`, `uhuh`, or `hmm` backchannels share one bounded FIFO arbiter,
so their outbound synthetic-mic audio never overlaps. The default pending limit
is 8 (`--companion-audio-queue-max`); overflow is rejected and counted. Meeting
switch, companion disconnect, or tab reattach clears queued audio and stops
active in-page sources. `/health` exposes this as `companionAudio`, including
readiness, queued/speaking state, capacity, counters, and last output/error.
Speech artifact IDs, agent/source markers, expected text, and the playback
window are also carried into companion-heard STT suppression.

The Silences admin page owns global, per-channel, and named test silence-action
configuration. Channel and test records are partial patches; deleting one
immediately reveals the current global values. Live meetings resolve channel
over global over built-in values. A Count-to-20 or ABC harness run temporarily
leases its named test patch above that live channel; stop/completion removes the
lease and an abandoned lease expires after five seconds. Test configuration
never launches or joins a meeting and never rewrites production settings.
One **Configuration target** selector lists **Default Config**, the currently
selected **Tests here** harness profile, then known channels. **Save override**
stores only differences from the current global default, **Reset from defaults**
deletes a test/channel patch (or reloads the saved global form), and **Save to
defaults** promotes the displayed effective form to global while preserving all
partial override patches. Restoring built-ins is a separate advanced action. **On
silence** uses caption stasis and/or incoming-audio quiet detection, acts once
on the quiet edge, and rearms after caption growth or speech resumes. `Continue`
opens a meeting-scoped floor gate in the shared agent TTS queue and never
synthesizes filler audio; if no utterance is held, the gate remains available
until one eligible agent queues. `Say "uh"`, `Say "uhuh"`, and `Say "hmm"` use
the companion audio arbiter and do not open the floor. `Say nothing` explicitly
records a suppressed no-op without floor, audio, backchannel, or caption-row
effects. All five choices use one `action` field and the same inherited trigger
and detector settings; audio shaping remains saved but applies only to `say:*`.
**Every N seconds** is valid for `nothing` and `say:*`, but not `continue`; both
modes honor the minimum safety gap and queue state. Switching meetings or
reattaching a tab invalidates queued output and resets scheduling state.

Meet frequently revises the punctuation and wording at the end of its active
caption row. The bridge therefore keeps the entire active row buffered until it
stops changing for `--settle` seconds. It releases the buffer immediately if
Meet advances to a newer row or removes the old row.

Caption DOM updates are delivered primarily over a CDP push channel:
`Runtime.addBinding("__wsCollabCaptionPush")` installs a page-side
`MutationObserver` that sends the same payload shape as the `/captions` polling
reader. Polling remains as a safety net and slows down while each active role is
receiving recent push frames. `/captions` and `/health` expose
`captionTransport`, `lastPushAt`, `lastPushIso`, `pushFrameCount`, and
`captionTransportByRole` so consumers can see whether HOST and COMPANION are
currently on `push` or `poll`.

If CDP bindings ever stop working, the documented fallback is a page-side
WebSocket to the existing server endpoint `ws://127.0.0.1:8802/ws_collab/ws`.
That alternative is not enabled by default because Google Meet's CSP blocks
localhost `connect-src`; it would require `Page.setBypassCSP` before injecting
the page WebSocket client.

Each finalized caption is pushed into `/ws_collab/v1/stt/ingest` with engine
`google_meet`, so the STT page and durable transcript stream identify Meet as
the source. Browser Web Speech remains a separate optional microphone test.

### Repeatable turn-taking trials

The hardware-free end-to-end scenarios alternate agent/user turns for counting
(`one` through `twenty`, odd turns spoken by the agent) and the alphabet (`A`
through `Z`, odd positions spoken by the agent):

```
.\.venv\Scripts\python.exe -m pytest -q tests\test_realtime_turn_taking.py
```

They are simulations, not claims of a live meeting. They route production TTS
to the companion destination and bounded companion arbiter, play it through a
fake companion backend, then feed fake companion remote-media segments through
the production STT/disambiguation/classifier/event path. Marked agent echoes are
ingested and must be rejected. Reports are JSON-serializable and contain each
expected/observed actor and token, latency and error category, p50/p95/max, and
drop/duplicate/misattribution/echo counts.

An operator can later run the same scenario against an **already running** Meet:

```
$env:WS_COLLAB_TOKEN = "<worker-or-admin-token>"
.\.venv\Scripts\python.exe -m ws_collab.realtime_live count `
  --meeting-url "https://meet.google.com/..." `
  --user-caption-name "Exact Meet caption display name" --confirm-live
```

Use `alphabet` instead of `count` for A-Z. The command never launches or joins
a meeting. It refuses to proceed unless the URL matches, bridge SSO preflight is
satisfied, configured HOST and COMPANION identities are verified, the companion
is in-call, and companion audio is ready. It never derives role assignment from
an account name: bridge configuration supplies roles, while
`--user-caption-name` is an explicit caption filter. On every human turn it
prints exactly which named person should say which token and waits for operator
input before checking the live caption.

### Companion-heard audio into Whisper and other STT drivers

#### Feedback-safe two-cable wiring

The Silences page can persist and apply four exact machine endpoints. RECEIVE
uses a virtual cable's browser playback side (often product-labeled “Input”)
and its paired server recording side (often product-labeled “Output”).
TRANSMIT must be a different virtual cable: its server playback endpoint
receives serialized agent TTS/backchannels and its browser recording endpoint
replaces the companion's outbound audio sender.

Wiring begins only after the companion is `in-call`. Remote media is first
muted, every live media element is assigned and verified against the exact
normalized RECEIVE browser label, server capture is started and verified, and
only then are those elements unmuted into that virtual sink. The mic is acquired
with an exact browser device constraint and replaces the existing sender; TTS
output is re-verified by host API, label, direction, and live index. Any zero or
ambiguous browser-label match, capture failure, changed media element, sink, or
track immediately restores mute and stops the receive capture. No default
speaker or host-mic fallback is used.

Receive-cable PCM drives both the secondary non-Meet STT fanout and the actual
Silence RMS/VAD decision. While cable mode is selected the legacy muted
MediaStream tap is disabled, preventing double feed. An unwired/failed cable
reports audio `not-ready` and cannot produce an audio-silence edge. Automatic
wiring is bounded to three post-join attempts per tab/meeting; **Wire now** can
apply or re-apply safely afterward. A manual disconnect suppresses automatic
rewiring only for that exact tab, meeting, and saved configuration revision;
an explicit wire, changed configuration, replacement tab, or new meeting clears
the suppression.

This route is experimental and remains **off by default**. Enable it for a
manual bridge with `--companion-heard-stt`, or for server-managed bridge
launches with `WS_COLLAB_COMPANION_HEARD_STT=1` (audio capture must also be
enabled with `WS_COLLAB_AUDIO_ENABLED=1`).

The companion's `audio`/`video` elements remain muted at volume zero. The
bridge taps their underlying remote `MediaStream`, sends bounded PCM batches to
the shared secondary-capture input, and that input fans out to every configured
non-Meet STT engine (for example Whisper and Vosk). It never feeds Meet caption
text into this route. `/audio/secondary-capture`, bridge `/health` under
`companionHeardStt`, and the existing Devices secondary-capture panel expose
connection, frame/byte/segment, drop, disconnect, and reconnect counters.
Companion synthetic-mic `/say` and click windows are excluded before ingestion.
The old virtual-cable `--companion-listen-device` options are retained only for
CLI compatibility and no longer make companion playback audible.

The Chrome/CDP automation remains in a child worker because it has blocking
browser and audio loops. Its loopback-only port (`48699` by default) is an
internal implementation detail, so the UI does not need direct access and a
worker failure cannot take down the REST/WebSocket server.

`ws_collab.drivers.stt.google_meet` polls `/captions` for whatever wall-clock
window an `AudioSegment` covers and resolves it through the normal
disambiguator/timeline pipeline, exactly like a native engine. The admin
UI's **Google Meet** page (deep ops view: HOST+COMPANION connector rows,
per-meeting captions/debug) and **Meet Bridge** page (a simpler live
transcript + join/new front door) both consume the authenticated server API.

## Mailbox integration

Unlike the original design (which imported a sibling plugin's in-process
mailbox client), the bridge talks to ws_collab's own `/v1/mailbox` REST API
over loopback HTTP (`--mailbox-base`, default the local server). Finished
captions post to `conversation` by default (`--to` to change); the bridge
watches the `google-meet` mailbox (`--outbox`) for `/join`, `/new`, `/say`,
and any other text (which is typed into the Meet chat). Native `mailbox_send`
has no free-form metadata field, so the original's per-message metadata
(`key`/`final`/`replaces`/`meetingUrl`) is folded into the visible text
instead of silently dropped.

## Limitations

- Windows-only (Windows SAPI for TTS, MME/DirectSound/WASAPI/WDM-KS device
  enumeration for the virtual-cable path; `--browser-backend wsl` still uses
  Windows-side TTS/audio, only the browser itself moves into WSL2).
- A CLIENT/GUEST mode (single account, no host authority, joins meetings
  other people host) is designed but not built.
- Meet's caption DOM is scraped via stable ARIA semantics, not fixed class
  names, but Google can still change behavior between releases; the admin
  UI's debug/"other things" log surfaces autojoin/mic-select verdicts so a
  regression is visible quickly rather than silently dropping captions.
## Authentication consent

All Google or Discord authentication navigation is classified and gated in the
central browser navigator, regardless of whether CDP or a future browser backend
performs the action. Explicit typed SSO intent is always required. Native
confirmation is disabled by default and can be enabled globally with **Require
confirmation before opening identity-provider sign-in pages** on the SSO /
Browser admin page. When disabled, explicitly intended SSO actions can navigate
without a dialog and emit a `consent-disabled` lifecycle record; all navigation
remains logged. When enabled, the operator must approve the native Python dialog.
Closing the dialog, its 30-second timeout, a dialog failure, or a noninteractive
session denies the action without opening a page.

Account scans use one short-lived approval scoped to the exact provider, Chrome
profile, typed scan intent, and scan operation. That approval covers the scan's
bounded `authuser` probes only; it cannot authorize another profile, provider,
intent, or later scan. Routine health/status reads use cached SSO state and
never scan or prompt. In the admin UI, **Scan signed-in accounts** is the
explicit operator action that can request this bounded consent when confirmation
is enabled. Running server-managed bridges read the persisted setting before
each authentication navigation, so toggles apply without restarting the bridge.
