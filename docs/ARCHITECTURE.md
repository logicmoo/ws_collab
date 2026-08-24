# WS_COLLAB architecture

## Layering

```
              ┌──────────────────────────────────────────────┐
   REST ──────▶                                              │
              │           WsCollabService                    │
    WS  ──────▶   (validation · identity · cursors ·         │
              │    idempotency · filters · auditing)         │
              └───────┬────────────────┬─────────────────────┘
                      │                │
        ┌─────────────▼──────┐   ┌─────▼─────────────────────┐
        │ durable event store│   │ subsystems                │
        │ (JSONL streams)    │   │ workers · capture · STT · │
        │ + cursor manager   │   │ disambiguator · classify ·│
        └────────────────────┘   │ TTS voices/queue · prompt │
                                 └───────────────────────────┘
```

`ws_collab/rest.py` and `ws_collab/ws.py` are thin transports. Both hold the same
`AppContext` (`ws_collab/context.py`), which owns exactly one
`WsCollabService`. This is why parity is structural: there is no second code path
where the two transports could diverge.

## Modules

| Module | Responsibility |
| --- | --- |
| `config.py` | `WS_COLLAB_*` settings, validation, the single writable state root |
| `events.py` | Event model, stream registry, semantic stream roles |
| `ids.py` | Sortable event ids and opaque cursor encoding |
| `jsonl_store.py` | Durable append-only streams, rotation, retention, recovery |
| `cursors.py` | Persistent per-consumer checkpoints with audited repositioning |
| `security.py` | Tokens, roles, sessions, CSRF, origins, allowlist, limits, redaction |
| `notify.py` | Publish/subscribe bridge from synchronous writes to async consumers |
| `service.py` | The shared service layer used identically by both transports |
| `rest.py` / `ws.py` | HTTP(S) and WS(S) transports |
| `server.py` | App factory, socket binding, startup report |
| `workers.py` | Worker registry, health thresholds, alerts, recovery |
| `audio/` | Device enumeration, routing matrix, VAD, capture service |
| `stt/` | Adapter contract, engine assembly, concurrent runner |
| `disambiguator.py` | Final transcript resolution (deterministic + optional LLM) |
| `classify.py` | Operator / agent / system-TTS classification and echo filtering |
| `tts/` | Voice catalog, per-agent profiles, speech queue, accuracy metrics |
| `prompt.py` | Versioned worker prompt with diff and rollback |
| `drivers/` | Drop-in STT and TTS engine directories |
| `admin/` | The operations workbench (static SPA) |

## Event model

Every durable record is an `Event`:

| Field | Meaning |
| --- | --- |
| `id` | Globally unique, time-sortable identifier |
| `stream` | Which durable stream it belongs to |
| `seq` | Monotonic position within that stream |
| `type` | Event type (e.g. `HEARD_SPEECH`) |
| `ts` | UTC timestamp |
| `schema_version` | Schema version of this record |
| `source_id` / `source_kind` | Who produced it |
| `correlation_id` | Ties one utterance together across streams |
| `idempotency_key` | Optional client-supplied de-duplication key |
| `data` | Type-specific payload |

Unknown top-level fields read from older records are preserved and re-emitted.

## Streams and roles

Streams are registered in `events.py`. **Resolve streams by role, not by name** —
roles are published through `/ws_collab/v1/capabilities` so a stream can be
renamed or split without breaking clients, the admin UI, or tests.

| Role | Purpose |
| --- | --- |
| `conversation` | Worker/agent/human messages |
| `worker_status` | Worker check-ins |
| `speech_pipeline` | Everything needed to render the unified transcript |
| `resolved_speech` | Listening, VAD, heard speech, resolved transcripts |
| `stt_hypotheses` | Raw per-engine hypotheses and the resolved result |
| `tts_queue` | Speech queue, playback, and accuracy evaluations |
| `audio_config` | Device and routing changes |
| `alerts` / `audit` / `diagnostics` | Operational streams |
| `prompt_history` | Versioned worker prompt |

## Correlated event flow

One `correlation_id` links an utterance end to end:

1. `LISTENING_STARTED` — capture begins on a selected device.
2. `SPEECH_DETECTED` — VAD (or an injected utterance) yields a segment.
3. Three STT engines run concurrently, each bounded by its own timeout; partials
   are `STT_PARTIAL_RESULT`, finals are `STT_FINAL_RESULT`, failures are
   `STT_ENGINE_ERROR`. One engine failing never cancels the others.
4. `TRANSCRIPT_RESOLVED` — the disambiguator appends one resolved transcript,
   preserving every raw hypothesis and any remaining uncertainty.
5. Source classification decides operator / agent / system-TTS / external /
   unknown, recording confidence and reasons.
6. If it is TTS echo: `TTS_AUDIO_DETECTED_BY_MICROPHONE`, an accuracy evaluation
   (`TTS_TRANSCRIPTION_EVALUATED`), and `TRANSCRIPT_FILTERED`. Echo is preserved
   diagnostically but excluded from command execution, which is what prevents a
   TTS → STT → TTS feedback loop.
7. `HEARD_SPEECH` — the resolved utterance, its segment, and its classification.
8. Both transports notify participants immediately.
9. A worker replies into `conversation`.
10. The reply is queued to that agent's voice; playback emits `TTS_STARTED`,
    `AGENT_SPEECH_STARTED`, then `TTS_FINISHED`.
11. The admin timeline renders all of it in one chronological view.

## Concurrency

* The event store is synchronous and guarded by a per-stream lock plus a
  directory-level single-writer lock.
* `notify.Broker` delivers events to each subscriber's asyncio queue using that
  subscriber's own loop, so no cross-thread races occur.
* Slow WebSocket consumers are dropped from the live feed rather than blocking
  others; they resynchronise from their durable cursor.
* The only internal loops are the server's own event loops, the TTS queue worker,
  and a bounded worker-health monitor. Nothing keeps external agents alive.
