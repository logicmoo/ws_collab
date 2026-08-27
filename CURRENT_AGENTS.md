# CURRENT_AGENTS

| Agent (session) | Model | Mode | Repository | Branch | Working directory | Services |
|---|---|---|---|---|---|---|
| Workbench plugins and EMU worker | `gpt-5.3-codex` | Autopilot | `logicmoo/symbolic_learner_arc3_kaggle_starter` | `main` | `…\symbolic_learner_workbench` | ws_collab :8802, mailbox relay :46667 (consumer) |
| Github remote setup (ws_collab) | `claude-fable-5` | Interactive | `logicmoo/ws_collab` | `master` | `…\workbench\plugins\ws_collab` | repo setup, LF normalization, mailbox_chat git repair |
| Snet mailbox connection (mailbox_chat) | `claude-fable-5` | Interactive | `logicmoo/mailbox_chat` | `teamspoon-snet-mailbox-connection` | `…\copilot-worktrees\mailbox_chat\teamspoon-automatic-train` | mailbox relay :46667 (operator); bridges: SNET Mattermost, Libera `##logicmoo`, Discord PrologMUD |
| Emullm websocket worker | `claude-fable-5` | Autopilot | `TeamSPoon/emullm` | `master` | `…\workbench\plugins\emullm` | emullm :8801, ws_collab :8802 |
| Agent registry update (ws_collab) | `gpt-5.6-sol` | Interactive | `logicmoo/ws_collab` | `master` | `…\workbench\plugins\ws_collab` | agent registry coordination; building agent inside sibling `task_harness_pl` (Prolog) plugin |
| Emullm websocket worker (emullm relay) | `claude-fable-5` | Autopilot | `TeamSPoon/emullm` | `master` | `…\workbench\plugins\emullm` | emullm relay :8801 (worker `codex-ide-1`), ws_collab :8802 consumer, EMU monitor |

Details for each agent follow below.

## Copilot/Codex worker (workbench plugins and EMU session)

- Worker: GitHub Copilot CLI assistant
- Model: GPT-5.3-Codex (`gpt-5.3-codex`)
- Session name: `Workbench plugins and EMU worker`
- Project session ID: `d1f67fa3-f8a9-467e-b53b-b2361ab99fe3`
- Mode: Autopilot

### Working directory and repository

- Working directory: `C:\snet\PeTTa\repos\symbolic_learner_workbench`
- Repository root: `C:\snet\PeTTa\repos\symbolic_learner_workbench`
- Repository: `logicmoo/symbolic_learner_arc3_kaggle_starter`
- Current branch: `main`
- Operating system: `Windows_NT`

### Endpoints and services

- WS_COLLAB standalone: `http://127.0.0.1:8802/ws_collab/v1`
- Mailbox relay standalone: `http://127.0.0.1:46667/v1`

### Recent work and notes

- (none recorded)

---

## Copilot worker (ws_collab session)

- Worker: GitHub Copilot CLI assistant (Copilot App branch session)
- Model: Claude Fable 5 (`claude-fable-5`)
- CLI version: `1.0.80`
- Session name: `Github remote setup`
- Session ID: `21257a17-5ef2-42e2-a777-752e01ff3afd`
- Project session ID: `ffa5f224-ab9b-4efe-aba9-7d19785cec00`
- GitHub account: `TeamSPoon` (via `gh` CLI)
- Mode: Interactive

### Working directory and repository

- Working directory: `C:\snet\PeTTa\repos\symbolic_learner_workbench\workbench\plugins\ws_collab`
- Repository root: `C:\snet\PeTTa\repos\symbolic_learner_workbench\workbench\plugins\ws_collab`
- Repository: `logicmoo/ws_collab`
- Current branch: `master`
- Operating system: `Windows_NT`
- Runtime shell: PowerShell on Windows
- Active workspace type: in-place branch workspace

### Endpoints and services

- No services operated; this worker does repo/git maintenance for `ws_collab`
  and sibling plugins on request.

### Recent work and notes

- Created public repo `logicmoo/ws_collab`, pushed `master`, set description
  ("Collaboration server for multiple agents using the same workspace").
- Enforced LF line endings in ws_collab (`.gitattributes`, `.editorconfig`,
  `.vscode/settings.json`); renormalized all files and pushed.
- Repaired `mailbox_chat` (sibling plugin): corrupt objects blocked pushes
  (`treeNotSorted` + pack CRC errors). Rebuilt all 117 commits via
  `git filter-repo` so the package path was always `src/mailbox_chat/`;
  force-pushed `main` (`75371b1`). Corrupt `.git` backup: `%TEMP%\mbx_backup\old_git`.
- Conventions in effect: LF line endings everywhere locally
  (`core.autocrlf=false`, `core.eol=lf`); commits include
  `Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>`.
- Registry coordination: honored write-hold requested by emullm worker
  (`bfee1a6e`) via cross-session message until its append completed; verified
  concurrent appends merged without data loss (lock check + SHA256 handshake).
- Entry updated: `2026-08-26T05:08Z` (local 13:08 +08:00).

---

## Copilot worker (mailbox_chat / SNET bridge session)

- Worker: GitHub Copilot CLI assistant
- Model: Claude Fable 5 (`claude-fable-5`)
- CLI version: `1.0.80`
- Session name: `Snet mailbox connection`
- Project session ID: `414ead72-5269-4d0d-86e5-f43287ef0971`
- Mode: Interactive

### Working directory and repository

- Working directory: `C:\snet\PeTTa\repos\coplilot_storage\copilot-worktrees\mailbox_chat\teamspoon-automatic-train`
- Repository root: `C:\snet\PeTTa\repos\symbolic_learner_workbench\workbench\plugins\mailbox_chat` (canonical)
- Repository: `logicmoo/mailbox_chat`
- Current branch: `teamspoon-snet-mailbox-connection` (worktree; base `main` @ 75371b1)
- Rescue branch: `rescue-mailbox-channels` (pre-rewrite mailbox_channels tree)
- Operating system: `Windows_NT`

### Endpoints and services

- Mailbox relay: `http://127.0.0.1:46667/v1` (mailbox-server-loop, restart via `POST /api/restart`)
- Bridge: SNET Mattermost (chat.singularitynet.io, 2 channels + DMs)
- Bridge: Libera IRC `##logicmoo` (nick prologmud)
- Bridge: Discord bot **PrologMUD** (server frdcsa-logicmoo-agi, #prologmud_bot_testing)

### Recent work and notes

- Current task: implement the ws_collab mailbox v1 API (`/ws_collab/v1`) into mailbox_chat.

---

## Copilot/Codex worker (emullm relay session)

- Coordinator: GitHub Copilot CLI assistant (Copilot App in-place branch session)
- Model: Claude Fable 5 (`claude-fable-5`) — switched from `gpt-5.6-sol` at 2026-08-26T05:19Z
- CLI version: `1.0.80`
- Session name: `Emullm websocket worker`
- Session ID: `bfee1a6e-3eec-462c-8563-0b127d910bd0`
- Project session ID: `aca791a9-7bf3-472a-8a60-29933ffcbcfd`
- Mode: Autopilot
- Relay worker ID: `codex-ide-1`
- Codex CLI: `codex-cli 0.149.0` (configured model `gpt-5.6-sol`)

### Working directory and repository

- Working directory: `C:\snet\PeTTa\repos\symbolic_learner_workbench\workbench\plugins\emullm`
- Repository root: `C:\snet\PeTTa\repos\symbolic_learner_workbench\workbench\plugins\emullm`
- Repository: `TeamSPoon/emullm`
- Current branch: `master`
- Workspace type: in-place branch workspace
- Operating system: `Windows_NT`

### Endpoints and services

- emullm HTTP: `http://127.0.0.1:8801`
- emullm worker WebSocket: `ws://127.0.0.1:8801/llm_emul/codex-ide-1/ws`
- WS_COLLAB REST: `http://127.0.0.1:8802/ws_collab/v1`
- WS_COLLAB WebSocket: `ws://127.0.0.1:8802/ws_collab/ws`
- Activity channel: `ws_collab/conversation`

### Persistent duty and state

- Primary task: puppet the emullm relay, answer routed model requests, and report
  non-sensitive activity to WS_COLLAB.
- EMU duties: monitor heard audio, monitor team conversation, publish truthful
  status, and detect/recover unresponsive workers.
- Supervisor identity: `copilot-emullm-monitor` (`on-activation` cadence).
- Runtime handoff files: `runtime\codex-ide-1-request.json` and
  `runtime\codex-ide-1-reply.json`.
- Persistent EMU state: `runtime\codex-ide-1-emu-state.json`.
- Entry appended: `2026-08-26T05:06:32Z`.

---

## Copilot worker (ws_collab agent registry session)

- Worker: GitHub Copilot CLI assistant (Copilot App in-place branch session)
- Model: GPT-5.6 Sol (`gpt-5.6-sol`)
- CLI version: `1.0.80`
- Session name: `Agent registry update`
- Project session ID: `9c125d7c-3cc6-45b2-aae5-a0a438e5f98e`
- Mode: Interactive

### Working directory and repository

- Working directory: `C:\snet\PeTTa\repos\symbolic_learner_workbench\workbench\plugins\ws_collab`
- Repository root: `C:\snet\PeTTa\repos\symbolic_learner_workbench\workbench\plugins\ws_collab`
- Repository: `logicmoo/ws_collab`
- Current branch: `master`
- Workspace type: in-place branch workspace
- Operating system: `Windows_NT`

### Endpoints and services

- (none in `ws_collab` itself)
- Sibling plugin `task_harness_pl` (SWI-Prolog coding-agent harness, not
  in this repo): REST API `http://127.0.0.1:8840/` (default; configurable),
  managed by its own `plugin.py` lifecycle hooks
  (start/stop/health via `POST /shutdown`).

### Recent work and notes

- Coordinated safe, append-only updates to `CURRENT_AGENTS.md`.
- **Heads-up for other agents/coordinators**: an agent is being
  implemented *inside* the sibling `task_harness_pl` Prolog plugin
  (`…\workbench\plugins\task_harness_pl`, own git-independent
  directory, not the `ws_collab` repo). It exposes a JSON REST API
  (`codex_harness_server.pl` + `codex_harness_server_main.pl` runner)
  so a host/workbench process can create, drive, observe, and tear
  down harness instances (`/harnesses`, `/harnesses/<id>/run|cancel|
  reset|tools/<name>`, etc.) and control its own lifecycle
  (`plugin.json` `plugin-lifecycle`/`plugin-api`, backed by `plugin.py`).
  No changes were made to `ws_collab` code/tests as part of that work;
  this note only informs the registry so ws_collab-side integration
  (e.g. a future mailbox/worker bridge for `task_harness_pl`) can be
  planned with awareness that this harness+REST surface now exists.
- Entry appended: `2026-08-26T13:07:48+08:00`.
- Entry updated: `2026-08-27T01:51:29+08:00`.
