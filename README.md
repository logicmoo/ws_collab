# WS_COLLAB

Coordination, audio, transcription, and administration infrastructure for
Codex/Copilot workers, built on durable JSONL event streams with full REST and
WebSocket parity.

WS_COLLAB runs two ways:

* **As a standalone server (recommended)** — `python -m ws_collab.standalone` binds
  HTTP/HTTPS/WS/WSS and prints a startup report. A host application (e.g. the
  workbench) then mounts it with a lightweight HTTP `web_proxy` at `/ws_collab`,
  so the host never imports WS_COLLAB and stays free of its dependencies.
* **As an in-process workbench plugin** — `plugin.json` + `plugin.py` include the
  router directly into the host app under `/ws_collab`. This is supported and works,
  but it is **not tested as thoroughly** as the standalone path, and — because the
  shared service layer eagerly wires up the audio, STT, and TTS subsystems —
  importing it **pulls the full WS_COLLAB dependency stack into the host process**.
  With the in-process path the host venv must carry whatever optional extras you
  enable, and model-backed STT engines (`whisper`, `vosk`, and especially `nemo`,
  which brings torch) can **download multi-gigabyte models**. Prefer the standalone
  + `web_proxy` deployment, run from its **own venv**, so the host/workbench venv
  stays clean.

Dependencies stay light by default: the base install is only
`fastapi`/`uvicorn`/`starlette` (no ML libraries, no model downloads). Everything
hardware/model specific is an opt-in extra (`audio`, `vosk`, `whisper`, `nemo`,
`sapi`, …); the `all` extra deliberately **excludes `nemo`**. The bundled TTS
backends are `fake` (default, hardware-free) and `sapi` (Windows built-in) — **no
TTS backend downloads models**.

```
  microphone ─▶ VAD ─▶ segment ─┬─▶ STT engine A ─┐
                                ├─▶ STT engine B ─┼─▶ disambiguator ─▶ resolved transcript
                                └─▶ STT engine C ─┘          │
                                                             ▼
     durable JSONL streams ◀── source classification / echo filtering
             │                                               │
     ┌───────┴────────┐                                      ▼
     ▼                ▼                              per-agent TTS queue
   REST clients   WS clients ─────▶ admin workbench ─────────┘
```

**Chrome Captions** is a separate push source, not an
`AudioSegment` driver. Every loaded page at `/ws_collab/captioner/` registers a
stable browser-session UUID and a per-load tab UUID. The backend selects exactly
one fresh, enabled instance; only that page may acquire the Web Lock, microphone,
RMS VAD, and Chrome Web Speech recognition. It pushes revisioned interim/final captions into
the same transcript stream. Chrome may send microphone audio to Google/cloud;
this source is not offline or local recognition. The same page separately uses
Web Audio to measure 20ms-frame microphone RMS for acoustic pause detection.
Those VAD samples stay in the browser and are neither transmitted nor stored by
WS_COLLAB; only bounded pause/transition timestamps and status scalars are sent.
Authenticated `speech_start`/`speech_end` transitions drive a source-scoped local
microphone floor. Conversational TTS and companion backchannels re-check that
floor immediately before playback, cancel cancellable active output on speech
onset, and wait through a 350ms clear hangover. Missing VAD expires fail-open.
Floor release never generates speech by itself.
Google Meet captions remain
meeting/chat context and do not participate in STT voting or `HEARD_SPEECH`.
The captioner alone autostarts with WS_COLLAB. It requires a normal browser tab:
the supervisor uses the dedicated `chrome_captioner` profile and its own CDP
port (`WS_COLLAB_CAPTIONER_CDP_PORT`, default `9224`). It never reads, copies,
or inherits a Meet SSO profile and never opens a Google login URL. **Isolated
browser profile: no Google login is used or inherited.** When that profile is
already available, the supervisor
opens/reuses one background tab where CDP supports it; otherwise it launches a
new visible Chrome window without headless mode. The first microphone permission
grant must be visible. Afterward the tab may remain backgrounded or the window
minimized, subject to Chrome/OS throttling. Use **Open** or **Foreground** on the
dedicated **Chrome Captions** admin page when operator attention is needed.

The canonical admin SPA is `/ws_collab/` (for example,
`/ws_collab/#chrome-captions`; `/ws_collab/admin/` remains an alias). Its
always-visible **Caption source priority** actions manage a durable generic
registry for Chrome Captions and Google Meet. One enabled source is primary.
Chrome primary suppresses only recent exact or conservative containment
duplicates from typed Meet conversation publication. Meet primary acknowledges
Chrome delivery as suppressed before canonical publication while retaining
bounded raw final delivery state and hash-only audit metadata. Disabling either
source is reversible and does not stop its bridge/profile or erase history.
`Disable other STT(s)` remains separate and bypasses configured `AudioSegment` engines while capture/VAD and manual
external ingest remain available.

Input scope is microphone only. Other tabs are not captured directly, and RMS
cannot distinguish a person from speaker audio leaking acoustically into the
mic. Use headphones or separate virtual audio devices. On Chrome desktop 135+
the page passes its live microphone track to `SpeechRecognition.start(track)`,
so recognition and silence detection use the same input and processing.
Recognition does not silently fall back to a separate default input if the
shared track is unavailable.

### ChatBot Test

Open `/ws_collab/#chatbot-test` to configure a registered conversational agent
using the existing emullm service (`http://127.0.0.1:8801/v1`, model
`emullm/default`). Its editable system prompt and per-agent history determine
the submitted context; the request preview makes that context visible.

Once enabled, it monitors finalized, non-echo speech from the shared STT
pipeline automatically. It does not open another microphone. Replies appear
incrementally and are spoken by the active browser client's text-to-speech
voice, assigned automatically and changeable in the page. Stop/Interrupt affects
this chat, not the general captioner. Keep the chat client open for spoken
replies; headphones help prevent acoustic feedback. This client submits chat
context only; the external emullm agent retains its own action permissions.
The agent waits for its full response and actual TTS completion before accepting
speech again; microphone activity is not treated as an automatic interruption.
The timing trace shows where response latency occurs. Workers can send direct
announcements through `/ws_collab/language-chat/agent-speech`, using the same
output gate and echo handling without an extra LLM round trip.
Model IDs and endpoint URLs are free text; the model list only offers suggestions.
You can draft either field while chat is active or temporarily disconnected.
Stop chat and **Save agent settings** to apply a draft; polling does not replace it.

Google Meet is a manual-start chat/transcript resource and is not in the current
autostart set. Start or join it explicitly through the operator controls,
`/join`, `/new`, or the bridge CLI.

## Quick start

```bash
export WS_COLLAB_ADMIN_TOKEN=choose-a-strong-token
python -m ws_collab.standalone 127.0.0.1 8802
```

Then open <http://127.0.0.1:8802/ws_collab/admin> and sign in with that token.

Public URLs have one namespace: REST is `/ws_collab/*`, WebSocket is
`/ws_collab/ws`, the admin UI is `/ws_collab/admin/*`, and OpenAPI is beneath
`/ws_collab/openapi`. Root and versioned aliases are intentionally not
mounted. `GET /ws_collab/endpoints` returns the categorized inventory.

If you do not configure a token, a random administrator token is generated and
written to `collab_state/generated_admin_token.txt` — it is never printed.

### Command-line and plugin startup

From this repository on Windows:

```powershell
.\.venv\Scripts\python.exe -m ws_collab.standalone start
.\.venv\Scripts\python.exe -m ws_collab.standalone status
.\.venv\Scripts\python.exe -m ws_collab.standalone restart
.\.venv\Scripts\python.exe -m ws_collab.standalone shutdown
```

The installed `ws-collab-standalone` command accepts the same actions.
`start` detaches and waits for HTTP readiness; `run` (or the original positional
form above) stays in the foreground. Use `--host`, `--port`, `--state-dir`, and
`--timeout` on control commands as needed. Default state/logs are in this repo's
`collab_state`, not `src\collab_state`. Restart launches a fresh Python server
after the previous one exits. It never restarts emullm or the Meet bridge.

The plugin host can call **`plugin.start_server()`** even after shutdown.
It returns a JSON-compatible result with `status`, `url`, `boot_id`, and
`supervisor_pid` (null when reusing an existing server). Optional keywords are
`host`, `port`, `state_dir`, `timeout`, and `python_executable`; pass the plugin's
own virtualenv interpreter when the host uses a different Python environment.
This is a local Python API, not an HTTP route on a stopped server; the host must
authorize any remote/UI action it exposes. Embedded mode remains host-owned.

Restart/shutdown controls are also visible in the admin top bar and **System &
Audit**. Protected CLI requests use `WS_COLLAB_TOKEN`, `WS_COLLAB_ADMIN_TOKEN`,
or the selected state's `generated_admin_token.txt`; never put tokens in command
arguments. See [operations](docs/OPERATIONS.md#shutdown-and-restart).

## The single writable directory

`collab_state/` is the **only** directory WS_COLLAB writes to; everything else can
be mounted read-only. Relocate it with `WS_COLLAB_STATE_DIR`. See
[`collab_state/README.md`](collab_state/README.md) for its contents.
The admin workbench persists each page's controls, preferences, rendered
snapshot, and latest API snapshots in `collab_state/admin_ui_state.json`;
credentials and authentication state are excluded.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, shared service layer, event flow |
| [docs/API.md](docs/API.md) | Every REST endpoint and WebSocket frame |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | All `WS_COLLAB_*` settings and deployment profiles |
| [docs/AUDIO.md](docs/AUDIO.md) | Devices, routing, STT drivers, TTS voices, accuracy |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Cursor recovery, worker monitoring, troubleshooting |
| [tests/README.md](tests/README.md) | The anti-calcification testing contract |

## Design commitments

* **Transport parity.** Every essential capability works through REST alone and
  through WS/WSS alone. Nothing is WebSocket-only. Both transports share one
  service layer, so identity, cursors, idempotency, filters, validation, and
  auditing cannot drift apart.
* **Durable by default.** JSONL streams are append-only with monotonic positions,
  crash-safe recovery, partial-line tolerance, rotation, and retention. Unknown
  fields on older records are preserved, never dropped.
* **Movable cursors.** Cursors are checkpoints, not barriers. Rewinding (replay)
  and skipping forward each require explicit authorization and are audited with
  the operator, reason, and risk.
* **No authentication bypass.** Tokens and roles are always enforced;
  administration is loopback-only unless remote access is deliberately enabled
  over TLS.
* **Honest degradation.** A missing model, device, or voice produces a reported
  fallback — never a silent substitution or an invented transcript. Real audio
  hardware, platform voices, and real speech models are used when present; the
  hardware-free doubles take over (and say so) when they are not.
* **Drop-in drivers.** STT and TTS engines live in their own directories under
  `ws_collab/drivers/` and are enumerated at startup. Rename a directory to
  `*_disabled` (or delete it) to remove an engine.
* **No worker keep-alive loops.** Native Codex/Copilot automation is the only
  approved recurring launcher; each activation runs one bounded monitoring cycle.

## Acknowledgement

The browser captioner is a clean-room implementation inspired by platform API
and architecture concepts in
[MidCamp/live-captioning at commit 893ebc75e9847dbb055963875822cf9b6afb94b8](https://github.com/MidCamp/live-captioning/tree/893ebc75e9847dbb055963875822cf9b6afb94b8).
No upstream source code, assets, or styles were copied. The upstream project is
GPL-3.0; this acknowledgement does not describe this implementation as a Chrome
extension or as local/offline STT.

## Running the tests

```bash
python -m pytest tests -q
```

The suite pins the hardware-free backends explicitly, so it needs no hardware,
credentials, paid APIs, or network access — and its results do not change based
on what happens to be installed on the machine.

## Optional: the real audio stack

```bash
pip install sounddevice soundfile faster-whisper vosk pywin32   # pywin32: Windows only
```

With these present WS_COLLAB uses the machine's real microphones, real speech
recognizers, and real platform voices. Without them everything still runs on the
doubles, and `capabilities.warnings` reports what degraded.
