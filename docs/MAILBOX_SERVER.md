# Mailbox server contract

A **mailbox server** is a "place" that hosts a directory of **mailboxes**. Every
ws_collab durable JSONL stream is a mailbox; the same contract is intended to be
implemented by the workbench so a federated chat can talk to both.

A client (e.g. the workbench `ChatConversation`) keeps a list of places, requests
each place's directory, and merges them — each mailbox tagged by its place.

All paths below are under the REST mount `/ws_collab/v1` (also mounted at
`/ws_collab`). On loopback, auth is disabled and every caller is a local admin.

## Endpoints

| Method | Endpoint | Role | Purpose |
|---|---|---|---|
| GET | `/mailbox/mailboxes` | viewer | Directory of this place's mailboxes (built-ins + dynamic); **omits hidden** |
| GET | `/mailbox/agents` | viewer | Identities (operator + workers) for the YOU/TO pickers |
| GET | `/capabilities` | viewer | Advertises `mailboxes`, `rest_base`, transports, features |
| GET | `/mailbox/messages` | viewer | Read a mailbox; params `mailbox,from,to,send_to,text,filter,limit` (YOU→`from`) |
| GET | `/streams/{mailbox}/tail` | viewer | Raw last-N events of a mailbox |
| WS  | `/ws_collab/ws` | viewer | Live: send `auth` then `subscribe {streams:[mailbox]}` |
| POST | `/mailbox/send` | worker | Post to the topic named by the mailbox; body `{to,text,sender,send_to}` |
| POST | `/mailbox/record` | operator | Edit a record = append a correction (append-only); body `{id,record,mode}` |
| POST | `/mailbox/create` | worker | Host a new mailbox; body `{id,purpose?,hidden?,source?}` |
| POST | `/mailbox/mailboxes` | worker | Alias of `create` (classic "add mailbox") |
| POST | `/mailbox/delete` | operator | Stop hosting a dynamic mailbox; body `{id}` |
| DELETE | `/mailbox/mailboxes?id=` | operator | Same as `delete` (RESTful form) |
| GET·POST | `/mailbox/mailbox-config` | viewer·operator | Read / set a per-mailbox config object |
| GET·POST·DELETE | `/mailbox/cursor` | viewer·operator | Inspect / move / clear an agent's cursor |
| POST | `/mailbox/subscription` | operator | Set / clear an agent's subscription intent |
| POST | `/mailbox/agents` | worker | Create/update an agent (user) with arbitrary properties `{id, properties}` |
| POST | `/mailbox/entity` | operator | Edit an agent / mailbox registry object |

### Resource × method matrix

| Resource | GET | POST | PUT | DELETE |
|---|---|---|---|---|
| `/mailbox/mailboxes` | list (omits hidden) | create *(alias)* | — | delete `?id=` |
| `/mailbox/create` | — | create | — | — |
| `/mailbox/delete` | — | delete | — | — |
| `/mailbox/messages` | read + filter | — | — | — |
| `/mailbox/send` | — | post to topic | — | — |
| `/mailbox/record` | — | edit (append) | — | — |
| `/mailbox/agents` | list | add | — | — |
| `/mailbox/cursor` | inspect | move | — | clear |
| `/mailbox/mailbox-config` | read | set | — | — |
| `/mailbox/subscription` | — | set/clear | — | — |
| `/mailbox/entity` | — | edit | — | — |
| `/streams/{mailbox}/tail` | raw tail | — | — | — |
| `/capabilities` | advertises mailboxes | — | — | — |

`PUT` is unused today; updates go through `POST` (`record` for messages,
`mailbox-config` for settings). A PUT alias can be added for strict REST later.

## Mailbox descriptor (a directory entry)

```json
{
  "id": "conversation",
  "name": "conversation",
  "purpose": "Shared chat and coordination between humans and agents.",
  "kind": "stream",
  "source": "jsonl",
  "transports": ["jsonl", "ws"],
  "hidden": false,
  "writable": true,
  "messages": 16,
  "filename": "conversation.jsonl",
  "endpoints": {
    "read": "/ws_collab/v1/mailbox/messages?mailbox=conversation",
    "send": "/ws_collab/v1/mailbox/send",
    "tail": "/ws_collab/v1/streams/conversation/tail",
    "ws":   "/ws_collab/ws"
  }
}
```

The directory response is `{ "place": "ws_collab", "mailboxes": [...], "server_time": "..." }`.

## Message shape

```json
{
  "id": "...", "timestamp": "...",
  "from": "<sender>", "to": "<recipient or mailbox>", "send_to": null,
  "text": "...", "type": "CONVERSATION_MESSAGE",
  "mailboxId": "<mailbox>", "mailboxName": "<mailbox>",
  "author": "<sender>", "authorName": "<sender>",
  "raw": { /* full underlying event */ }
}
```

`send_to: null` means the message's own mailbox. On send, the destination topic
is the first mailbox name among `send_to`, then `to`, else `conversation`; a
distinct recipient is kept in `data.to`.

## Writable and virtual mailboxes

* Each mailbox declares `writable`. Built-in streams and dynamic mailboxes are
  writable by default; a dynamic mailbox may be created read-only with
  `writable: false`. Posting (`send`) or editing (`record`) a non-writable
  mailbox is refused (a `send` simply falls back to `conversation`).
* A server may **emulate** read-only mailboxes that project internal state as a
  JSONL stream instead of a durable file. ws_collab exposes `server-agents`
  (`source: "virtual"`, `writable: false`): reading it returns the agents/users
  directory, one agent per message. Its `raw` field is the full agent record.

## Agents (users)

`GET /mailbox/agents` is the users/identity directory: the operator, registered
workers, agents in the durable registry, and any distinct `source_id`s seen in
the conversation. Each entry carries arbitrary properties (e.g. `display_name`,
`color`, `role`, `voice`), and the server hosts a per-agent cursor per mailbox.

`POST /mailbox/agents {id, properties}` creates/updates an agent's arbitrary
properties (persisted in `agents.json`). The same directory is also readable as
the emulated `server-agents` mailbox.

## Dynamic and hidden mailboxes

* Clients create mailboxes with `POST /mailbox/create`; the server begins hosting
  a new durable JSONL stream by that name. Built-in streams cannot be created or
  deleted (HTTP 409).
* Names must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.
* `hidden: true` omits the mailbox from `GET /mailbox/mailboxes`. It remains fully
  usable by name — so an unguessable name (e.g.
  `conversation-9f3a1c-set-tokens-to-even-know-the-name`) is the capability
  required to read or post to it.
* Dynamic mailboxes persist in `collab_state/mailboxes.json` and are rehydrated
  on restart.

## WebSocket / live

Connect `/ws_collab/ws`, send `{"type":"auth","token":"<any on loopback>"}`, then
`{"type":"subscribe","streams":["<mailbox>", ...],"cursors":{}}`. Events arrive as
`{"type":"event","event":{...}}`. The server sends a liveness `{"type":"ping"}`
**server→client**; there is no client→server pong handler, so clients should not
reply pong. Cursors are durable — keep the last cursor to resume.

## Roles

GET endpoints require `viewer`; `send`/`create`/`agents` require `worker`;
`delete`/`record`/`cursor`(POST/DELETE)/`mailbox-config`(POST)/`subscription`/
`entity` require `operator`. On loopback with auth disabled, every caller
satisfies all roles.
