# WS_COLLAB API

Everything below works over plain HTTP and over HTTPS. Every capability also has
a WebSocket equivalent — nothing is WebSocket-only, and nothing is REST-only.

## Canonical URL policy

REST resources are unversioned beneath one canonical namespace. No operation is
mounted at multiple paths.

| Surface | Canonical path |
| --- | --- |
| REST (discovery, auth, events, mailbox, workers, audio, STT/TTS, Meet, cursors, prompt, diagnostics) | `/ws_collab/*` |
| Full-parity WebSocket | `/ws_collab/ws` |
| Admin SPA and assets (canonical) | `/ws_collab/` |
| Admin SPA compatibility alias and operator lifecycle controls | `/ws_collab/admin/*` |
| Dedicated browser captioner page and scoped internal delivery | `/ws_collab/captioner/*` |
| OpenAPI UI / ReDoc / schema | `/ws_collab/openapi/docs`, `/ws_collab/openapi/redoc`, `/ws_collab/openapi.json` |
| Internal Meet bridge loopback API (port 48699) | `/ws_collab/meet-bridge/*` |

Root aliases and `/v1/*` are intentionally not mounted and
return 404. Fetch
the machine-readable categorized inventory from
`GET /ws_collab/endpoints`; its `rest.endpoints` list is generated from
the running router and includes each canonical path and HTTP method.

## Authentication

Send a bearer token:

```bash
curl -H "Authorization: Bearer $WS_COLLAB_ADMIN_TOKEN" \
     http://127.0.0.1:8802/ws_collab/capabilities
```

Or exchange a token for a cookie session (used by the admin page):

```
POST /ws_collab/auth/login    {"token": "..."}   -> sets cookie, returns csrf
POST /ws_collab/auth/logout
GET  /ws_collab/auth/whoami
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
GET /ws_collab/events?stream=conversation&after=<cursor>&limit=100
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
  "http://127.0.0.1:8802/ws_collab/events?stream=conversation&after=$CURSOR&wait_ms=25000"
```

Bounded history without cursors: `GET /ws_collab/streams/{stream}/tail?count=200`.

## Writing events

```bash
curl -X POST http://127.0.0.1:8802/ws_collab/conversation/events \
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

Generic form: `POST /ws_collab/events` with `{"stream", "type", "data",
"correlation_id", "idempotency_key"}`.

## Endpoint reference

### Discovery
| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/health` | public |
| GET | `/ws_collab/capabilities` | public |
| GET | `/ws_collab/config` | viewer |
| GET | `/ws_collab/diagnostics` | viewer |
| GET | `/ws_collab/audit` | operator |

### Conversation and events
| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/events` | viewer |
| POST | `/ws_collab/events` | worker |
| GET | `/ws_collab/streams/{stream}/tail` | viewer |
| GET | `/ws_collab/conversation` | viewer |
| POST | `/ws_collab/conversation/events` | worker |

### Browser navigation
| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/browser/nav-intents?after=<cursor>&limit=100` | viewer |
| POST | `/ws_collab/browser/nav-intents` | worker |
| GET | `/ws_collab/meet/browser-settings` | viewer |
| POST | `/ws_collab/meet/browser-settings` | operator |
| GET | `/ws_collab/meet/companion-cable-wiring` | viewer |
| POST | `/ws_collab/meet/companion-cable-wiring` | operator |
| POST | `/ws_collab/meet/companion-cable-wiring/wire` | operator |
| POST | `/ws_collab/meet/companion-cable-wiring/disconnect` | operator |
| GET | `/ws_collab/meet/routing?meeting_url=<room>` | viewer |
| POST | `/ws_collab/meet/routing` | operator |
| POST | `/ws_collab/meet/routing/sync` | operator |
| GET | `/ws_collab/meet/channels` | viewer |
| POST | `/ws_collab/meet/channels/forget` | operator |
| POST | `/ws_collab/meet/channels/prune` | operator |

The POST route ingests redacted intent/outcome records from browser worker
processes. Both phases share a `nav_id`; GET returns the durable `events` page
and a newest-first `records` view merged by that identifier.

Companion cable wiring persists four exact machine endpoints: RECEIVE browser
playback/server capture and a different TRANSMIT TTS playback/companion mic
pair. Supplying `meeting_url` stores a meeting override in
`meet_browser_settings.json`; otherwise the old `sound_settings.json` value is
the explicit global default. Reads report `scope: meeting|global-default`.
Saving never applies or unmutes it.
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
role/Silence/routing settings and test leases, but preserve transcript/event
history. A forgotten meeting therefore cannot remain eligible as the default
for an explicit bridge start or for reconnect.

Meeting routing policies are versioned and keyed by normalized Meet URL in the
active browser profile. They contain `room_adapter`, per-role `mic` and
`speakers` descriptors (`label`, normalized label, and last device ID),
`default_on_bridge_start`, `reconnect_after_disconnect`, and an optional meeting-scoped
companion wiring override. Updating one policy is lock-protected and atomically
replaces the settings file. Enabling `default_on_bridge_start` clears it on every other
meeting in the same transaction. Legacy stored `autostart` values are preserved
and treated only as this explicit-start default; no routing policy starts the
Meet bridge during service startup. The adapter registry currently exposes only
`physical_computer` as available; Discord, Zoom, and plain audio-call entries
are capability records marked unavailable rather than integrations.

`POST /meet/routing/sync` accepts `meeting_url` and `role` (`host` or
`companion`). It re-resolves exact normalized browser labels, rejects blank,
ambiguous, default, unavailable, mismatched, or ineligible devices, applies
both devices to that role's current controlled Meet tab, and succeeds only
after the mic and speaker report verified state. Companion sync additionally
validates/applies both distinct cable pairs and starts the RECEIVE server
capture feeding secondary Silence/STT.

### Workers
| Method | Path | Role |
| --- | --- | --- |
| POST | `/ws_collab/workers/register` | worker |
| POST | `/ws_collab/workers/{id}/status` | worker |
| GET | `/ws_collab/workers` | viewer |
| POST | `/ws_collab/workers/monitor` | operator |
| GET | `/ws_collab/alerts` | viewer |

### Audio
| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/audio/capture` | viewer |
| POST | `/ws_collab/audio/capture/start` · `/stop` | operator |
| GET | `/ws_collab/audio/secondary-capture` | viewer |
| POST | `/ws_collab/audio/secondary-capture/start` · `/stop` | operator |
| POST | `/ws_collab/audio/secondary-capture/browser` | operator |
| POST | `/ws_collab/audio/utterance` | operator |
| GET | `/ws_collab/audio/devices` | viewer |
| POST | `/ws_collab/audio/devices/refresh` | operator |
| GET/POST | `/ws_collab/audio/routing` | viewer / operator |

### Transcription
| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/stt/transcripts` | viewer |
| POST | `/ws_collab/stt/ingest` | worker |
| GET | `/ws_collab/transcripts` | viewer |

The push-driven `browser_captioner` is distinct from audio-segment STT engines:

| Method | Path | Authentication |
| --- | --- | --- |
| GET | `/ws_collab/captioner/` | strict loopback only (unaffected by `admin_remote`) |
| GET/POST | `/ws_collab/captioner/config` | viewer / operator |
| GET | `/ws_collab/captioner/status` | viewer |
| GET | `/ws_collab/caption-sources` | viewer |
| POST | `/ws_collab/caption-sources/{browser_captioner,google_meet}/{enable,disable,make-primary}` | operator |
| GET | `/ws_collab/captioner/instances` | viewer |
| POST | `/ws_collab/captioner/instances/{instance_id}/{enable,disable,make-primary}` | operator |
| POST | `/ws_collab/captioner/{open,focus,pause,resume}` | operator |
| POST | `/ws_collab/captioner/{ingest,heartbeat,control}` | loopback + same-origin + scoped per-process token |
| POST | `/ws_collab/captioner/vad-transition` | loopback + same-origin + scoped token + current lease-owner token |

The scoped token is embedded only in the no-store, loopback-only HTML bootstrap,
never in a URL, and is not a general worker bearer. Internal calls additionally
require a loopback peer, exact same-origin Host/Origin, and JSON content type.
Each page heartbeat binds its local-storage session UUID, per-load tab UUID, and
server boot ID. The bounded registry automatically keeps a fresh healthy
selection, preferring an already-selected instance and then listening candidates
by first-seen/ID order. Operator selection is pinned while fresh, even if
degraded, and becomes automatic only after the 15-second stale timeout.
Per-instance Disable survives heartbeat refresh for that tab instance but not a
tab reload (which creates a new instance UUID). Only a selected, enabled, fresh
instance receives microphone authority and a VAD owner token; Web Locks remain
an additional same-origin guard.
Ingest accepts one envelope or at most 50 `items`; each includes `session_id`,
the per-tab `instance_id`,
monotonic `seq`, `utterance_id`, `revision`, text, final flag, confidence,
language, and timestamps. Responses identify each acknowledged envelope in
`results` and retain `acked_seqs` plus `highest_contiguous` for compatibility.
Finalization is durably retried and its resolved/heard outputs are idempotent.
Final envelopes may include up to 128 strictly validated `pauses` entries:
`duration_ms` (20ms through 24h), RFC3339 `start_at`/`end_at`,
`source: "browser_rms_vad"`, `after_char` within final text, and alignment
`interim_prefix`, `between_utterances`, or `approximate_text_position`.
`silence_before_ms` remains as compatibility metadata for the last
between-utterance pause. Heartbeats expose only bounded VAD scalars (RMS, adaptive
noise floor/threshold, state, current silence, frame interval, and error), never
PCM. Chrome recognition may be cloud-backed; the separate pause VAD is local.
Heartbeat input metadata is strictly limited to `input_scope: microphone`, a
bounded track label, optional short device fingerprint, actual echo/noise/AGC
booleans, channel count, and sample rate. Raw browser `deviceId` and PCM are
rejected.

`vad-transition` has an independent 8KiB body limit and accepts exactly one
ordered metadata event: `speech_start`, `speech_end`, or recovery `state_sync`,
with RFC3339 time, positive epoch/sequence, `source: browser_rms_vad`,
`source_id: local_microphone`, and `input_scope: microphone`. The heartbeat
issues an ephemeral token only to the current browser lease owner. Duplicate
transitions are idempotent, older sequence/epoch transitions are rejected as
stale, old/future wall times are rejected, and the normal captioner rate limiter
still applies. Status and health expose clear/speech/hangover/unknown floor
state; unknown is fail-open.
The backend-authoritative source policy contains `browser_captioner` and
`google_meet`, an enabled flag for each, and zero or one primary source. Exactly
one enabled source is primary whenever any source is enabled. Disabling the
primary chooses the deterministic `browser_captioner`, then `google_meet`,
fallback. The old `prefer_over_google_meet`, `disable_google_meet`, and
captioner `enabled` booleans are migrated and mirrored for compatibility.
`disable_other_stts` remains a separate audio-STT policy. The scoped
captioner-page token cannot change source or instance policy. Config/status also identify the dedicated
`chrome_captioner` profile and CDP endpoint; that profile is isolated from every
Meet SSO profile and has no inherited Google login.

With Chrome primary, a finalized typed Meet caption that exactly matches or
safely contains/is contained by a recent Chrome final is acknowledged but not
published to `conversation`. With Meet primary, Chrome batches are acknowledged
as suppressed before canonical STT/floor publication; final raw delivery remains
in the bounded durable captioner state and audit stores a text hash, reason, and
identity rather than text. With Meet disabled, all typed Meet captions are
similarly acknowledged and suppressed. Both cases write idempotent, text-free
`MEET_CAPTION_SUPPRESSED` audit metadata. With other STTs disabled, captured
audio/VAD continues but configured audio engines and disambiguation are bypassed
and a text-free `STT_SEGMENT_SKIPPED` diagnostic is emitted. Manual
`/stt/ingest` remains available unless its `engine` names a configured audio
engine.

`stt/ingest` is the bridge for an **external recognizer** (for example a desktop
app's dictation engine). The transcript is recorded as a hypothesis and, when
final, flows through the same disambiguation, classification, and timeline path
as a local engine:

```bash
curl -X POST http://127.0.0.1:8802/ws_collab/stt/ingest \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"engine": "external-asr", "text": "deploy the staging build", "confidence": 0.94}'
```

### Speech output
| Method | Path | Role |
| --- | --- | --- |
| POST | `/ws_collab/tts/speak` | worker |
| GET | `/ws_collab/tts` | viewer |
| POST | `/ws_collab/tts/cancel` | operator |
| POST | `/ws_collab/tts/measure` | operator |
| GET | `/ws_collab/tts/accuracy` | viewer |
| GET | `/ws_collab/voices` | viewer |
| POST | `/ws_collab/voices/{agent_id}` | operator |
| POST | `/ws_collab/voices/assign` | operator |

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

`GET`, `POST`, and `DELETE /ws_collab/meet/companion-click` retain the
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
| GET | `/ws_collab/cursors` | viewer |
| GET | `/ws_collab/cursors/{stream}/{consumer}` | viewer |
| GET | `/ws_collab/cursors/{stream}/{consumer}/history` | viewer |
| POST | `/ws_collab/cursors/{stream}/{consumer}/commit` | worker |
| POST | `/ws_collab/cursors/{stream}/{consumer}/reposition` | operator |
| POST | `/ws_collab/cursors/{stream}/{consumer}/reset` | operator |

Repositioning backwards requires `"allow_replay": true`; forwards requires
`"allow_skip": true`. Both are refused otherwise, and both are audited.

### Prompt
| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/prompt` · `/prompt/history` | viewer |
| POST | `/ws_collab/prompt` | operator |
| POST | `/ws_collab/prompt/preview-diff` | operator |
| POST | `/ws_collab/prompt/rollback` | operator |

### Administration
`GET /ws_collab/admin` — loopback-only unless `WS_COLLAB_ADMIN_REMOTE=1` (which
requires TLS).

| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/admin/ui-state/{page}` | viewer |
| POST | `/ws_collab/admin/ui-state/{page}` | operator |
| POST | `/ws_collab/admin/shutdown` | operator |
| POST | `/ws_collab/admin/restart` | operator |

The page-state endpoint stores JSON snapshots in
`collab_state/admin_ui_state.json`. Credential-like fields are removed before
the atomic write.

Lifecycle controls acknowledge before acting and permit only one pending action.
They return `409` when actions conflict or when an embedded host has not supplied
the corresponding lifecycle callback.

### Google Meet bridge

The authenticated server owns the Chrome/CDP worker and proxies its internal
loopback API. `POST /meet/bridge/command` starts the worker automatically for
`/join <url>` and `/new` when it is offline.

| Method | Path | Role |
| --- | --- | --- |
| GET | `/ws_collab/meet/bridge/status` | viewer |
| GET | `/ws_collab/meet/bridge/captions?since=<epoch>` | viewer |
| POST | `/ws_collab/meet/bridge/command` | operator |
| POST | `/ws_collab/meet/bridge/media-mute` | operator |
| POST | `/ws_collab/meet/routing/sync` | operator |
| POST | `/ws_collab/meet/bridge/start` | operator |
| POST | `/ws_collab/meet/captions/ingest` | worker |

The typed Meet caption route publishes durable `google_meet_caption`
conversation context with speaker, role, meeting URL/key, final/replacement, and
duplicate metadata. It does not create STT hypotheses, invoke disambiguation, or
emit `HEARD_SPEECH`.

`media-mute` accepts
`{"meeting_url":"https://meet.google.com/…","role":"host|companion","target":"mic|speakers","muted":true|false}`.
It controls the selected Meet tab, not WS_COLLAB's physical capture service.
The main-service routing sync is the supported device-selection operation. Its
authenticated internal worker leg is
`/ws_collab/meet-bridge/device-sync`; browsers cannot call that loopback
mutation directly.

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
