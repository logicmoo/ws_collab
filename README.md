# WS_COLLAB

Coordination, audio, transcription, and administration infrastructure for
Codex/Copilot workers, built on durable JSONL event streams with full REST and
WebSocket parity.

WS_COLLAB runs two ways:

* **As a workbench plugin** — `plugin.json` + `plugin.py` mount every route under
  `/ws_collab` in the host application.
* **As a standalone server** — `python -m ws_collab.server` binds HTTP/HTTPS/WS/WSS
  and prints a startup report.

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

## Quick start

```bash
export WS_COLLAB_ADMIN_TOKEN=choose-a-strong-token
python -m ws_collab.server 127.0.0.1 8802
```

Then open <http://127.0.0.1:8802/ws_collab/admin> and sign in with that token.

If you do not configure a token, a random administrator token is generated and
written to `collab_state/generated_admin_token.txt` — it is never printed.

## The single writable directory

`collab_state/` is the **only** directory WS_COLLAB writes to; everything else can
be mounted read-only. Relocate it with `WS_COLLAB_STATE_DIR`. See
[`collab_state/README.md`](collab_state/README.md) for its contents.

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
