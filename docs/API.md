# WS_COLLAB API

Everything below works over plain HTTP and over HTTPS. Every capability also has
a WebSocket equivalent — nothing is WebSocket-only, and nothing is REST-only.

* REST root: `/ws_collab`
* Versioned resources: `/ws_collab/v1/...`
* WebSocket: `/ws_collab/ws` (`ws://` or `wss://`)
* Interactive OpenAPI: `/docs` (standalone server)

## Authentication

Send a bearer token:

```bash
curl -H "Authorization: Bearer $WS_COLLAB_ADMIN_TOKEN" \
     http://127.0.0.1:8802/ws_collab/v1/capabilities
```

Or exchange a token for a cookie session (used by the admin page):

```
POST /ws_collab/v1/auth/login    {"token": "..."}   -> sets cookie, returns csrf
POST /ws_collab/v1/auth/logout
GET  /ws_collab/v1/auth/whoami
```

Cookie-authenticated mutations must send the CSRF token in
`X-WS-Collab-CSRF`. Bearer-token clients do not need CSRF.

### Roles

`viewer` < `worker` < `operator` < `admin`. Reads need `viewer`; publishing and
cursor commits need `worker`; configuration, cursor repositioning, prompt edits,
and audio control need `operator`.

## Errors

Every failure — on both transports — uses the same envelope and codes:

```json
{"error": {"code": "cursor_invalid", "message": "...", "details": {"recovery": "..."}}}
```

| Code | HTTP |
| --- | --- |
| `validation_error` | 400 |
| `authentication_required` | 401 |
| `forbidden` | 403 |
| `not_found` | 404 |
| `conflict` / `cursor_invalid` | 409 |
| `payload_too_large` | 413 |
| `rate_limited` | 429 |

## Reading events

```http
GET /ws_collab/v1/events?stream=conversation&after=<cursor>&limit=100
```

| Parameter | Meaning |
| --- | --- |
| `stream` | Stream name (resolve it from `capabilities.stream_roles`) |
| `after` | Opaque cursor; omit to start at the beginning |
| `limit` | 1–1000 |
| `wait_ms` | 0–30000; block server-side until an event arrives |
| `type`, `source_id`, `source_kind`, `correlation_id`, `since`, `until`, `q` | Filters |

Response:

```json
{
  "stream": "conversation",
  "events": [ ... ],
  "next_cursor": "opaque-token",
  "has_more": false,
  "server_time": "2026-01-01T00:00:00.000Z",
  "malformed": 0
}
```

`next_cursor` is also returned as an `ETag`. Sending it back as `If-None-Match`
together with `after` yields `304 Not Modified` instead of an empty page.

**REST clients never need a tight polling loop** — use `wait_ms`:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8802/ws_collab/v1/events?stream=conversation&after=$CURSOR&wait_ms=25000"
```

Bounded history without cursors: `GET /ws_collab/v1/streams/{stream}/tail?count=200`.

## Writing events

```bash
curl -X POST http://127.0.0.1:8802/ws_collab/v1/conversation/events \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"text": "cycle complete"}'
```

Returns the durable id, position, cursor, and duplicate status. Success is only
reported after the event is durably accepted:

```json
{"id": "01M0...", "seq": 42, "cursor": "...", "duplicate": false, "server_time": "..."}
```

Replaying the same `Idempotency-Key` returns the original id with
`"duplicate": true` and writes nothing.

Generic form: `POST /ws_collab/v1/events` with `{"stream", "type", "data",
"correlation_id", "idempotency_key"}`.

## Endpoint reference

### Discovery
| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/health` | public |
| GET | `/ws_collab/v1/capabilities` | public |
| GET | `/ws_collab/v1/config` | viewer |
| GET | `/ws_collab/v1/diagnostics` | viewer |
| GET | `/ws_collab/v1/audit` | operator |

### Conversation and events
| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/v1/events` | viewer |
| POST | `/ws_collab/v1/events` | worker |
| GET | `/ws_collab/v1/streams/{stream}/tail` | viewer |
| GET | `/ws_collab/v1/conversation` | viewer |
| POST | `/ws_collab/v1/conversation/events` | worker |

### Browser navigation
| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/v1/browser/nav-intents?after=<cursor>&limit=100` | viewer |
| POST | `/ws_collab/v1/browser/nav-intents` | worker |
| GET | `/ws_collab/v1/meet/browser-settings` | viewer |
| POST | `/ws_collab/v1/meet/browser-settings` | operator |
| GET | `/ws_collab/v1/meet/companion-cable-wiring` | viewer |
| POST | `/ws_collab/v1/meet/companion-cable-wiring` | operator |
| POST | `/ws_collab/v1/meet/companion-cable-wiring/wire` | operator |
| POST | `/ws_collab/v1/meet/companion-cable-wiring/disconnect` | operator |
| GET | `/ws_collab/v1/meet/channels` | viewer |
| POST | `/ws_collab/v1/meet/channels/forget` | operator |
| POST | `/ws_collab/v1/meet/channels/prune` | operator |

The POST route ingests redacted intent/outcome records from browser worker
processes. Both phases share a `nav_id`; GET returns the durable `events` page
and a newest-first `records` view merged by that identifier.

Companion cable wiring persists four exact machine endpoints in
`sound_settings.json`: RECEIVE browser playback/server capture and a different
TRANSMIT TTS playback/companion mic pair. Saving never applies or unmutes it.
`/wire` is the narrow authenticated proxy to the bridge's idempotent atomic
operation; `/disconnect` immediately mutes remote media and stops its capture.
The bridge accepts these operations only from the main server with its worker
credential, rejects browser origins and arbitrary endpoint overrides, and
re-fetches the validated saved configuration through the worker API.
The worker-only runtime/capture subroutes accept only the saved RECEIVE device
and are not general bridge-command or audio-device controls.

Meet browser settings include the global boolean `require_sso_consent` (default
`false`). The settings POST validates it as a JSON boolean. When enabled,
explicitly typed SSO navigation requires native operator confirmation; when
disabled, typed SSO navigation proceeds with a `consent-disabled` log record.
The typed-intent classifier remains mandatory in both modes.

Meeting forgetting stores normalized `forgotten_meeting_urls` tombstones in the
active Meet profile. Passive event, admin-state, browser-history, tab, and live
status discovery cannot restore a tombstoned channel. An explicit `/join`
clears its tombstone. `channels/prune` requires a non-empty `keep` URL array and
refuses to exclude the active meeting. Both operations remove channel-scoped
role/Silence settings and test leases, but preserve transcript/event history.

### Workers
| Method | Path | Role |
| --- | --- | --- |
| POST | `/ws_collab/v1/workers/register` | worker |
| POST | `/ws_collab/v1/workers/{id}/status` | worker |
| GET | `/ws_collab/v1/workers` | viewer |
| POST | `/ws_collab/v1/workers/monitor` | operator |
| GET | `/ws_collab/v1/alerts` | viewer |

### Audio
| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/v1/audio/capture` | viewer |
| POST | `/ws_collab/v1/audio/capture/start` · `/stop` | operator |
| GET | `/ws_collab/v1/audio/secondary-capture` | viewer |
| POST | `/ws_collab/v1/audio/secondary-capture/start` · `/stop` | operator |
| POST | `/ws_collab/v1/audio/secondary-capture/browser` | operator |
| POST | `/ws_collab/v1/audio/utterance` | operator |
| GET | `/ws_collab/v1/audio/devices` | viewer |
| POST | `/ws_collab/v1/audio/devices/refresh` | operator |
| GET/POST | `/ws_collab/v1/audio/routing` | viewer / operator |

### Transcription
| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/v1/stt/transcripts` | viewer |
| POST | `/ws_collab/v1/stt/ingest` | worker |
| GET | `/ws_collab/v1/transcripts` | viewer |

`stt/ingest` is the bridge for an **external recognizer** (for example a desktop
app's dictation engine). The transcript is recorded as a hypothesis and, when
final, flows through the same disambiguation, classification, and timeline path
as a local engine:

```bash
curl -X POST http://127.0.0.1:8802/ws_collab/v1/stt/ingest \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"engine": "external-asr", "text": "deploy the staging build", "confidence": 0.94}'
```

### Speech output
| Method | Path | Role |
| --- | --- | --- |
| POST | `/ws_collab/v1/tts/speak` | worker |
| GET | `/ws_collab/v1/tts` | viewer |
| POST | `/ws_collab/v1/tts/cancel` | operator |
| POST | `/ws_collab/v1/tts/measure` | operator |
| GET | `/ws_collab/v1/tts/accuracy` | viewer |
| GET | `/ws_collab/v1/voices` | viewer |
| POST | `/ws_collab/v1/voices/{agent_id}` | operator |
| POST | `/ws_collab/v1/voices/assign` | operator |

`POST /tts/speak` accepts `destination: "local" | "companion"` and optional
`meeting_url`. The default remains `local` for compatibility unless
`WS_COLLAB_TTS_OUTPUT_DESTINATION=companion` is configured. A companion request
is rejected unless the assigned COMPANION tab is attached, in the requested
active meeting, and its synthetic microphone is ready. The response includes the
resolved destination. `GET /tts` includes `destinations.companion` readiness,
queue/speaking state, bounded capacity, sent/completed/dropped/rejected counters,
and last utterance/error/destination. `POST /tts/cancel {"id": ...}` cancels both
the server queue and any matching companion output.

### Meet companion backchannels

`GET`, `POST`, and `DELETE /ws_collab/v1/meet/companion-click` retain the
historical route name. The Silences admin page is the sole editor. Pass
`meeting_url` to select an exact room; GET reports `source: "override"` or
`"default"` plus `globalDefault`.

Scoped clients pass `scope=global|channel|test`, a normalized Google Meet
`channel_key` for channel/test context, and `test_profile` for test scope.
Channel identities are returned as `google-meet:<room-code>` rather than a
display label. GET returns canonical values plus `scope`, `scopeKey`,
`effective`, `hasOverride`, the stored `override` patch, and per-field
`sources`. Resolution is test patch > channel patch > saved global > built-in.
Test profiles may be resolved without a channel; in that case their base is
global. A scoped POST accepts settings in `override` and can use
`replace_override: true`. Scoped DELETE removes the channel/test patch; global
DELETE resets only the saved global default to built-ins.

POST accepts `enabled`, `action` (`continue`, `nothing`, `say:uh`, `say:uhuh`,
or `say:hmm`), and
`mode` (`reactive` for **on silence**, `fixed` for **every N seconds**),
`interval_seconds`, `trigger` (`caption`, `audio`, or `both`),
`after_seconds`, `silence_ms`, `min_gap_seconds`, `max_wait_seconds`,
`audio_rms_threshold`, `click_ms`, `gain`, and formants `f0_hz`, `f1_hz`,
`f2_hz`. Legacy `phrase`/`sound` inputs remain accepted and normalize to
`action`; responses and new storage contain only `action`. DELETE with `meeting_url` removes
that room's override and restores inherited defaults.

`continue` is valid only with on-silence/reactive mode. It sends no companion
filler audio: the bridge posts one meeting/test-scoped edge to
`POST /meet/floor/continue`, which records `CONVERSATION_FLOOR_CONTINUE` and
opens/releases one held utterance in the normal agent TTS queue.
`POST /meet/floor/queue` holds an eligible companion utterance for that signal;
`GET /meet/floor/status` reports open/granted/deferred state, and DELETE
`/meet/floor` invalidates stale grants. `nothing` grants no floor and queues no
audio; it records one suppressed no-op evaluation per silence edge or configured
interval cadence. Interval mode accepts `nothing` and `say:*`, but rejects
`continue`. Phrase-only saved records resolve as `say:<phrase>` without rewriting
storage.

The observation harness leases a selected test profile onto its live channel
through `POST /meet/companion-click/test-session`. The UI renews the five-second
lease while running and DELETEs it on stop/completion, so a disconnected test
cannot remain active. This endpoint only selects configuration; it never
launches or joins a meeting.

### Cursors
| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/v1/cursors` | viewer |
| GET | `/ws_collab/v1/cursors/{stream}/{consumer}` | viewer |
| GET | `/ws_collab/v1/cursors/{stream}/{consumer}/history` | viewer |
| POST | `/ws_collab/v1/cursors/{stream}/{consumer}/commit` | worker |
| POST | `/ws_collab/v1/cursors/{stream}/{consumer}/reposition` | operator |
| POST | `/ws_collab/v1/cursors/{stream}/{consumer}/reset` | operator |

Repositioning backwards requires `"allow_replay": true`; forwards requires
`"allow_skip": true`. Both are refused otherwise, and both are audited.

### Prompt
| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/v1/prompt` · `/prompt/history` | viewer |
| POST | `/ws_collab/v1/prompt` | operator |
| POST | `/ws_collab/v1/prompt/preview-diff` | operator |
| POST | `/ws_collab/v1/prompt/rollback` | operator |

### Administration
`GET /ws_collab/admin` — loopback-only unless `WS_COLLAB_ADMIN_REMOTE=1` (which
requires TLS).

| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/v1/admin/ui-state/{page}` | viewer |
| POST | `/ws_collab/v1/admin/ui-state/{page}` | operator |

The page-state endpoint stores JSON snapshots in
`collab_state/admin_ui_state.json`. Credential-like fields are removed before
the atomic write.

### Google Meet bridge

The authenticated server owns the Chrome/CDP worker and proxies its internal
loopback API. `POST /meet/bridge/command` starts the worker automatically for
`/join <url>` and `/new` when it is offline.

| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/v1/meet/bridge/status` | viewer |
| GET | `/ws_collab/v1/meet/bridge/captions?since=<epoch>` | viewer |
| POST | `/ws_collab/v1/meet/bridge/command` | operator |
| POST | `/ws_collab/v1/meet/bridge/start` | operator |

## WebSocket protocol

Connect to `/ws_collab/ws`, then authenticate before anything else.

| Client frame | Purpose |
| --- | --- |
| `{"type":"auth","token":"..."}` | Authenticate; replies `auth_ok` with capabilities |
| `{"type":"subscribe","streams":[...],"cursors":{...},"filters":{...}}` | Subscribe with catch-up from a cursor |
| `{"type":"resume","streams":[...],"cursors":{...}}` | Resume from the last acknowledged cursor |
| `{"type":"unsubscribe","streams":[...]}` | Stop receiving those streams |
| `{"type":"publish","stream","event_type","data","idempotency_key","ack_id"}` | Publish; replies `ack` |
| `{"type":"stt_ingest", ...}` | External transcript ingest; replies `ingest_result` |
| `{"type":"cursor","action":"get\|commit\|reposition\|reset", ...}` | Cursor operations |
| `{"type":"ping"}` | Liveness; replies `pong` |

| Server frame | Meaning |
| --- | --- |
| `auth_ok` | Authenticated; includes capabilities |
| `subscribed` / `unsubscribed` | Subscription state changed |
| `event` | A durable event |
| `caught_up` | Historical catch-up finished for a stream; includes its cursor |
| `ack` | Durable acceptance of a publish |
| `cursor_result` / `ingest_result` | Command results |
| `ping` / `pong` | Liveness |
| `error` | Same code and message a REST call would return |

Catch-up and live delivery are gap-free and de-duplicated: the live subscription
is active before history replays, and each stream position is delivered once.

## Client modes

`examples/clients/` contains all three required modes:

* `rest_client.py` — REST only, cursor + long polling
* `ws_client.py` — WebSocket preferred with automatic REST fallback that
  preserves the cursor
* `copilot_speech_bridge.py` — pushing an external recognizer's speech in
