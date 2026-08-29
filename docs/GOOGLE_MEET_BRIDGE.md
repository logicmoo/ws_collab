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
keeps a **second** signed-in Google account (its own SSO profile, signed in
once) sitting **muted and deaf** in the call so Google always sees two
participants. The real (HOST) account's microphone is never touched by any
automation; the companion's "microphone" is a synthetic in-page WebAudio
source (or, if `--mic-select-device`/`--tts-output-device` are configured, a
real virtual-cable device) that only carries `/say` speech, never the room.

## Running it

```
pip install -e ".[meet,audio]"          # websocket-client, scipy + sounddevice/numpy
ws-collab-meet-bridge                    # ALWAYS-ON servant meeting
ws-collab-meet-bridge --meet <meet-url>  # join a given meeting
ws-collab-meet-bridge --companion        # + a second muted account
ws-collab-meet-bridge --list-audio-devices
```

First run: a Chrome window pops up with its own persistent profile -- pick
your Google account once (the SSO login persists across runs; `--forget-sso`
wipes it to switch accounts).

## Browser backend

By default the bridge launches a normal visible **Windows** Chrome/Edge window.
You can keep that unchanged, or switch the browser hosting backend:

```
ws-collab-meet-bridge --browser-backend windows   # default
ws-collab-meet-bridge --browser-backend wsl        # run Chrome inside WSL2 + Xvfb
ws-collab-meet-bridge --browser-backend wsl --wsl-distro Ubuntu-24.04
```

- `--browser-backend windows` keeps today's behavior: native Windows browser
  windows, visible on the desktop, foreground-able with `/foreground`.
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

## HTTP API (what ws_collab consumes)

The bridge exposes a small local, unauthenticated HTTP API on its own port
(default `48699`, `--status-port` to change):

- `GET /health` -- `{ok, service, meetingUrl, captionCount, lastCaptionAt,
  outbox, recipients, clients, debug}`.
- `GET /captions?since=<epoch>` -- every caption row (`key`, `at`,
  `updated_at`, `speaker`, `text`, `final`, `replaces`, `meetingUrl`) whose
  `updated_at` is newer than `since`.
- `POST /command {"command": "/join <url>" | "/new" | "/say <text>"}`.

`ws_collab.drivers.stt.google_meet` polls `/captions` for whatever wall-clock
window an `AudioSegment` covers and resolves it through the normal
disambiguator/timeline pipeline, exactly like a native engine. The admin
UI's **Google Meet** page (deep ops view: HOST+COMPANION connector rows,
per-meeting captions/debug) and **Meet Bridge** page (a simpler live
transcript + join/new front door) are both just HTTP consumers of this same
API -- as is any other tool that wants Meet's captions.

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
