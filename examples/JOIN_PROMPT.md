# Joining a WS_COLLAB session

Give the prompt below to any agent/worker **running on the same machine** as the
server. The server listens on `127.0.0.1:8802` (loopback only) and, for local
use, requires **no authentication** — every local caller is treated as an admin.

For agents on **other machines**, see "Remote join" at the bottom.

---

## Copy‑paste prompt

```text
You are joining a shared voice/collaboration session hosted by a local
"WS_COLLAB" server. Connect to it and participate as a worker.

SERVER
- Base URL:        http://127.0.0.1:8802
- REST base:       /ws_collab/v1   (also mounted at /ws_collab)
- WebSocket:       ws://127.0.0.1:8802/ws_collab/ws
- Auth:            NONE required on localhost (loopback). If a token is ever
                   required, send HTTP header  Authorization: Bearer <TOKEN>
                   and WS frame {"type":"auth","token":"<TOKEN>"}.
- Must run on THIS machine (server is loopback-only).

STEP 1 - Discover (confirm you can reach it)
  GET /status
  GET /ws_collab/v1/capabilities     -> lists streams, roles, features
  GET /ws_collab/v1/auth/whoami      -> your identity/role

STEP 2 - Register yourself as a worker
  POST /ws_collab/v1/workers/register
    {"worker_id":"<your-unique-id>","task":"<what you do>","meta":{}}
  Then check in periodically (heartbeat; call again on state changes):
  POST /ws_collab/v1/workers/<your-unique-id>/status
    {"status":"active","data":{},"errors":[]}

STEP 3 - Listen to the session (choose ONE)
  A) WebSocket (preferred, live):
     1. connect ws://127.0.0.1:8802/ws_collab/ws
     2. send {"type":"auth","token":"local"}          # any value when auth is off
     3. send {"type":"subscribe","streams":["conversation","stt_transcripts","tts_queue"],"cursors":{}}
     4. receive {"type":"event","event":{...}}; reply to {"type":"ping"} with {"type":"pong"}
  B) REST long-poll (fallback, no deps):
     GET /ws_collab/v1/conversation?after=<cursor>     # repeat with returned cursor
     (same pattern for /ws_collab/v1/stt/transcripts)

Key streams: conversation, stt_transcripts (heard speech), tts_queue (spoken),
worker_statuses, system_alerts. Cursors are durable - keep the last cursor to
resume without missing or duplicating events.

STEP 4 - Participate
  Post a chat/coordination message:
    POST /ws_collab/v1/conversation/events
      {"text":"hello, joining now","source_id":"<your-unique-id>","source_kind":"agent"}
  Speak out loud via the shared TTS:
    POST /ws_collab/v1/tts/speak
      {"agent_id":"<your-unique-id>","text":"ready to help","priority":5}
  Submit text you recognized (external speech-to-text):
    POST /ws_collab/v1/stt/ingest
      {"engine":"<your-name>","text":"...","confidence":0.9,"is_final":true,"resolve":true}

ETIQUETTE
- Use a stable, unique worker_id and set source_kind:"agent".
- Keep heartbeats flowing so you aren't flagged unresponsive.
- Read the conversation + transcripts before speaking; don't repeat others.

Minimal stdlib client to prove the connection:
  import json, urllib.request
  B="http://127.0.0.1:8802/ws_collab/v1"
  def call(path, body=None):
      r=urllib.request.Request(B+path,
          data=None if body is None else json.dumps(body).encode(),
          headers={"Content-Type":"application/json"},
          method="GET" if body is None else "POST")
      return json.load(urllib.request.urlopen(r))
  print(call("/capabilities")["product"])
  call("/workers/register", {"worker_id":"agent-1","task":"demo"})
  call("/conversation/events", {"text":"agent-1 online","source_id":"agent-1","source_kind":"agent"})
```

---

## Remote join (other machines)

The server only listens on loopback by default, so remote agents cannot reach it.
To allow remote join, restart the server bound to your LAN. Authentication turns
**back on automatically** when the bind is not loopback‑only, so also configure a
token:

```
WS_COLLAB_BIND_ADDRESSES=0.0.0.0
WS_COLLAB_DEV_INSECURE=1        # or configure WS_COLLAB_TLS_CERT_FILE/KEY_FILE
WS_COLLAB_TOKENS=join-token=worker
```

Remote agents then use `http://<your-ip>:8802` and send
`Authorization: Bearer join-token` on every request (and
`{"type":"auth","token":"join-token"}` on the WebSocket).

## Roles

`viewer` (read) < `worker` (post conversation/status, ingest, speak) <
`operator` (devices/routing/cancel) < `admin`. On localhost with auth disabled
every caller is `admin`.
