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
  http://127.0.0.1:8802/ws_collab/cursors/conversation/worker-1

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
  http://127.0.0.1:8802/ws_collab/workers/monitor
```

## The worker prompt

`long_running_prompt.txt` is versioned. Saving is atomic, the previous text is
preserved, every version is appended to durable history, and any version can be
diffed or rolled back to (rollback creates a new version — history is
append-only). Edit it in the admin page or via
`/ws_collab/prompt`. The default text is in
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

Operators can use the confirmed controls in the **top bar** or **System & Audit**, or call
`POST /ws_collab/admin/shutdown` and `POST /ws_collab/admin/restart`. Both
require operator authorization and normal mutation CSRF/origin protection.
Embedded hosts return `409` unless they explicitly supply the corresponding
lifecycle callback.

Shutdown cancels the health monitor, drains and stops the TTS queue, closes
owned child processes, ends Uvicorn serving, and releases the state-directory
lock. Restart uses exit code `75` internally: `python -m ws_collab.standalone`
runs a supervisor loop which waits for complete shutdown before starting a fresh
server interpreter with the original arguments and environment. The supervisor
PID stays stable across restarts; the child server PID and `boot_id` change.
The store then re-derives
each stream's position from durable data, repairs an unterminated final record,
and continues without reusing a position. Consumers resume from persisted
cursors.
Connection/request draining is limited to ten seconds, after which Uvicorn
cancels remaining requests before service cleanup. A disconnected browser cannot
leave shutdown waiting indefinitely for its transport. Save pending edits first.

Use `python -m ws_collab.standalone start` to launch detached, `status` to inspect,
`restart` to wait for a different ready boot, and `shutdown` to wait for the
listener to close. These commands accept `--host`, `--port`, `--state-dir`, and
`--timeout`; `--help` lists them. `run` runs the supervised server in the
foreground and also accepts `--https-port`. The original positional foreground
form remains supported. `python -m ws_collab.server` / `ws-collab` is the
unsupervised child entrypoint: it exits with `75` on restart and needs an external
supervisor. Prefer `ws-collab-standalone` for operator use.

Startup only reuses a listener that returns the WS_COLLAB status schema and is
ready. An unrelated or unready listener is an explicit error, never a reason to
kill a process. A startup timeout reports the supervisor PID and
`collab_state\standalone.log`; the child may still be starting. CLI errors and
stopped/down status return a nonzero exit code. An already-stopped shutdown is
idempotent. A stopped service cannot restart itself over HTTP.

Hosts can use the exported local `plugin.start_server(...)` callable to start it
again after shutdown; it accepts host/port/state directory/timeout and an optional
`python_executable`. It defaults to the caller's interpreter, so a host using
another environment should explicitly pass this plugin's `.venv` interpreter.
This callable does not add an unauthenticated web route or assume an undocumented
host lifecycle-hook contract. The host is responsible for authorization of any
exposed action. It rejects embedded mode, where the host owns the service.

CLI authorization is read from `WS_COLLAB_TOKEN`, then `WS_COLLAB_ADMIN_TOKEN`,
then the state directory's generated token file. It uses the same operator-only
API as the UI and never follows redirects or sends local controls through an
HTTP proxy. Credentials are not printed or accepted as command arguments.
Model/endpoint/prompt/history and captioner policy survive restart; ChatBot Test
starts stopped on every new boot.
