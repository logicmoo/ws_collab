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
