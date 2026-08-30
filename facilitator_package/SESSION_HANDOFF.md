# Session Handoff — 2026-08-31 ~02:50

Written before a Copilot logout/relaunch. Read this first to resume.

## Role

I am acting as the **Facilitator** described in `BOOTSTRAP_PROMPT.md` / `FACILITATOR_LOOP.md`:
read the live Meet transcript, detect actionable requests, record them, **delegate every
implementation to exactly one sub-agent at a time**, and report a status table. The
facilitator does not write project code itself.

Tasks live in the session SQLite DB, table `todos`:
`C:\Users\dougl\.copilot\session-state\41e772b8-5624-4f24-b27d-01f0b6cdfe0d\session.db`
(NOT `facilitator_package/facilitator.db` — that was specified in the bootstrap prompt but
never actually created.)

## Repo state

- Repo: `logicmoo/ws_collab`, branch `master`
- Last commit: `9645396` "Require status table to print on sub-agent state changes"
- **27 modified files + 5 new files are UNCOMMITTED.** They persist on disk across a
  Copilot relaunch. Nothing is lost by logging out.
- Test suite was last green at **362 passed** (baseline at session start was 306).

New untracked files:
`src/ws_collab/admin/silences.js`, `tests/test_admin_meet_ui_static.py`,
`tests/test_admin_silences_ui_static.py`, `tests/test_meet_bridge_captions_payload.py`,
`tests/test_meet_cdp_tab_reuse.py`

## Services (verify before assuming)

- Admin server `127.0.0.1:8802` — `python -m ws_collab.standalone 127.0.0.1 8802`
- Meet worker `127.0.0.1:48699` — started ONLY via `POST /v1/meet/bridge/start`
  with `{"meeting_url": "https://meet.google.com/bgb-xqts-xjt"}`
- Meet Chrome CDP on `9223`, profile `C:\Users\dougl\.cache\ws_collab_models\meet_bridge_profile`

Restart recipe that works:
1. Find PIDs by port, `Stop-Process -Id <PID>` (PID only — name-based kills are blocked).
2. Kill leftover `ws_collab` python processes; a stale one can hold the JSONL writer lock
   and cause `ConflictError: another writer already owns this JSONL directory`.
3. Start admin server detached, wait for `/v1/status`.
4. `POST /v1/meet/bridge/start`, then poll `/v1/meet/bridge/status` until `ok: true`.

## What shipped this session

- **Captions from BOTH host and companion tabs** — separate `CaptionTracker` per role,
  role-prefixed keys (`host:` / `companion:`), cross-role de-dupe with `duplicateOf`.
- **CDP push transport** — `Runtime.addBinding("__wsCollabCaptionPush")` + injected
  `MutationObserver`; poll slows to 2s while push is healthy, auto-degrades to `"poll"`,
  self-heals. Status: `captionTransport`, `captionTransportByRole`, `pushFrameCount`.
- **Raw diagnostics** — `rawText` / `rawRows` / `rawByRole` with a two-level separator
  scheme: ` | ` between DOM children, ` \u2016 ` between rows. Per-entry `rawText` too.
- **`?fromEnd=N`** on `/v1/meet/bridge/captions`.
- **SSO no longer re-scans** — `ssoSatisfied` steady state; health/status is cache-only and
  never triggers a live scan.
- **Companion "uh" interjector** — silence-gated (caption stasis, 500ms) + monologue gate
  (10s) + min gap (6s), per-meeting on/off, `clicksSent` / `rowBreaksObserved` metrics.
  Purpose: force Meet to break a single continuously-growing caption row.
- **Silences admin page** — turn-taking test harness with pass/fail scoring.
- **Emit / Phrases / Transcribe** now populated, de-duplicated, with Role + Dup columns.
- Bug fixes: caption replay after restart, block-buffered worker logs (`-u` +
  `PYTHONUNBUFFERED` + `flush=True`), CDP frame corruption (bytes/empty frames fed to
  `json.loads`), invalid `%s` in observer JS, duplicate bridge process guard,
  recovered 2 lost meetings, suggestions reaching the workspace.

## In flight when the session ended

`companion-whisper-agent` was routing the companion's HEARD audio to Whisper/vosk and was
at the **validation** stage (131 tool calls). Its edits are on disk. It was ALSO asked to
fix `bridge.py:1722`, where `args.launch_url = SSO_SETUP_URL` is set unconditionally so
every worker launch opens a Google AccountChooser sign-in tab even when `ssoSatisfied`.
**Verify both are complete and the suite is green before continuing.**

## Open user tasks (see `todos` table for full text)

| id | title |
|---|---|
| `companion-output-to-whisper-program` | Companion's heard audio → Whisper + non-Meet drivers |
| `nav-intent-global-log` | Global navigation-intent log on the SSO/Browser page, with WHY |
| `sso-consent-dialog` | Python dialog box before opening ANY Google sign-in tab |
| `companion-click-ui` | Admin UI toggle for the companion click, per meeting |
| `agent-voices-via-companion` | Route agent TTS out through the companion's synthetic mic |
| `realtime-turn-taking-test` | Test 1 — alternating count to 20 |
| `realtime-abc-test` | Test 2 — alternating ABCs |

Design intent: the companion becomes the agent system's full-duplex I/O device — it HEARS
the meeting (into Whisper) and SPEAKS for the agents — while the host stays a pure human
seat whose mic is never touched.

## Standing constraints

- Never leave this workspace (`CO-IDE-WS`); ask before inspecting anything outside it.
- One code-mutating sub-agent at a time, unless file sets are provably disjoint.
- Never send a mutating `/command` directly to port `48699`.
- Do not restart live services unless asked.
- Commit only when explicitly asked.
- Both Google accounts display as "Douglas Miles"; roles are resolved by `authuser` +
  email, never by display name.
