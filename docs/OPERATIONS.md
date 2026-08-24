# Operations

## The admin workbench

`http://127.0.0.1:8802/ws_collab/admin` — loopback-only unless
`WS_COLLAB_ADMIN_REMOTE=1` (which requires TLS). Sign in with any configured
token; the page then works entirely over REST if WebSockets are blocked, and
shows its current transport (WSS, WS, HTTPS REST, HTTP REST, or disconnected) in
the top bar.

| Page | Contents |
| --- | --- |
| Unified Transcript | The full speech pipeline in chronological order |
| Conversation | Worker/agent/human messages; send a message |
| JSONL Streams | Any stream, rendered or raw, with filters and export |
| Workers | Registry, state, age, errors, last conversation, confirm-terminated |
| Alerts | Raised/recovered alerts with severity and scope |
| Devices & Routing | Enumerated devices, capture control, routing matrix |
| Agent Voices | Voice catalog, profiles, conflicts, preview, policies |
| TTS Accuracy | Rolling WER/CER by engine with worst examples |
| Cursors | Inspect and reposition cursors with explicit risk warnings |
| Prompt | Edit, preview diff, save a version, roll back |
| System & Audit | Health, streams, configuration, capabilities, audit history |

Stream views never load an unbounded file into browser memory: they use a bounded
virtualized buffer with a visible cap. **Clear view** clears only the browser —
durable data is untouched.

Autoscroll follows new events, pauses automatically when you scroll upward, and
shows a *Jump to latest* control with an unseen count. The view is never forced
downward while you are reading history.

High-volume controls: hide partials, low confidence, routine events, or TTS echo;
finals only; group by utterance; cap visible events; pause rendering while
retaining a bounded buffer; and see how many events are hidden.

## Cursor recovery

Cursors are checkpoints, not barriers. Each `(stream, consumer)` pair has its own
position, and every move records the old and new position, reason, operator,
timestamp, and risk.

```bash
# inspect
curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8802/ws_collab/v1/cursors/conversation/worker-1

# advance after successful processing
curl -X POST .../cursors/conversation/worker-1/commit \
  -d '{"token": "<cursor>", "reason": "processed"}'

# rewind to replay (explicitly accepting duplicate processing)
curl -X POST .../cursors/conversation/worker-1/reposition \
  -d '{"seq": 120, "reason": "incident replay", "allow_replay": true}'

# skip forward (explicitly accepting that events are missed)
curl -X POST .../cursors/conversation/worker-1/reposition \
  -d '{"seq": 900, "reason": "backlog drained out of band", "allow_skip": true}'

# reset after a stream was repaired or replaced
curl -X POST .../cursors/conversation/worker-1/reset \
  -d '{"to": "start", "reason": "stream restored from backup"}'
```

Rewinding without `allow_replay`, or skipping without `allow_skip`, is refused.
`commit` never moves backwards — use `reposition`.

### Recovering from a rejected cursor

A cursor that is malformed, from another stream, or beyond the end of a stream
raises `cursor_invalid` **with a usable `details.recovery` position**. Resume from
that instead of restarting from zero:

```json
{"error": {"code": "cursor_invalid",
           "message": "cursor is beyond the end of the stream",
           "details": {"recovery": "<usable-cursor>"}}}
```

Use event ids or idempotency keys for any external side effect so a replay cannot
duplicate it.

## Worker monitoring

Workers register, check in, and are classified `ok` → `warn` → `overdue` →
`unresponsive` from the configured thresholds. Transitions raise deduplicated
alerts, escalate as the situation worsens, and emit recovery events on check-in.
When every worker is quiet, a single team-wide failure alert is raised.

A quiet worker is reported **overdue** or **unresponsive**, never "terminated",
unless termination is independently confirmed — the last worker able to report may
be the only remaining observer.

Run one bounded cycle on demand:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8802/ws_collab/v1/workers/monitor
```

## The worker prompt

`long_running_prompt.txt` is versioned. Saving is atomic, the previous text is
preserved, every version is appended to durable history, and any version can be
diffed or rolled back to (rollback creates a new version — history is
append-only). Edit it in the admin page or via
`/ws_collab/v1/prompt`. The default text is in
[`examples/long_running_prompt.txt`](../examples/long_running_prompt.txt).

Its key constraint: **native Codex/Copilot automation is the only approved
recurring launcher.** OS schedulers, external launchers, watchdogs, self-revival
scripts, and scripted keep-alive/polling loops are prohibited. Each activation
performs one bounded monitoring cycle, then returns to primary work. Unsupported
timing requirements must be reported honestly rather than simulated with a loop.

(This prohibition is about keeping *agents* alive. The server's own event loops,
TTS queue worker, and bounded health monitor are normal and expected.)

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `401 authentication_required` | Missing/invalid token. Check `WS_COLLAB_TOKENS`, or read `collab_state/generated_admin_token.txt`. |
| `403 forbidden` on admin | Admin is loopback-only. Set `WS_COLLAB_ADMIN_REMOTE=1` **with** TLS. |
| `403 forbidden` on a mutation | Role too low (`viewer` < `worker` < `operator` < `admin`), or a cookie session missing `X-WS-Collab-CSRF`. |
| `409 conflict` at startup | Another process owns `collab_state/`. Stop it, or use a different `WS_COLLAB_STATE_DIR`. |
| `409 cursor_invalid` | Stream rotated, truncated, or replaced. Resume from `details.recovery`. |
| `429 rate_limited` | Raise `WS_COLLAB_RATE_LIMIT_RPS`, or use `wait_ms` long polling instead of tight polling. |
| Startup refuses to bind | Non-loopback without TLS. Configure `WS_COLLAB_TLS_*`, or set `WS_COLLAB_DEV_INSECURE=1` deliberately. |
| Capture will not start | `WS_COLLAB_AUDIO_ENABLED=1` is required. |
| An engine reports `timeout` | Raise `WS_COLLAB_STT_TIMEOUT_MS`; the other engines are unaffected. |
| An engine silently became a double | Its library/model is missing. Check `capabilities.warnings`. |
| A remote engine never runs | `WS_COLLAB_STT_ALLOW_REMOTE=1` is required before audio leaves the device. |
| Malformed-line markers appear | A writer crashed mid-record. The record is reported and skipped; the stream is intact. |
| Admin shows "REST fallback" | WebSockets are blocked upstream. Everything still works over REST. |
| Agent speaks with the wrong voice | Check the profile's `fallback` policy and `voice_resolution` in the speak response. |

## Shutdown and restart

Shutdown cancels the health monitor, drains and stops the TTS queue, closes
WebSocket subscriptions, and releases the state-directory lock. On restart the
store re-derives each stream's position from durable data (even if the recovery
sidecar is lost), repairs an unterminated final record, and continues without
reusing a position. Consumers resume from their persisted cursors.
