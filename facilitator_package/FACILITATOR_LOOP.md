# The Facilitator Loop

As mandated by BOOTSTRAP_PROMPT.md, the facilitator loop operates strictly as a single, bounded cycle invoked exclusively by the native Copilot CRON or Codex Automation system. **Shell loops, persistent processes, and blocking waits are strictly prohibited.**

## Single Cycle Execution

On each invocation, the facilitator performs the following steps in order, then exits immediately:

1. **Verify Environment:** Check `POLICY_AMENDMENTS.md`. Acquire/renew `locks/loop.lock`. If `PAUSE` exists or another cycle is active, release locks and exit.
2. **Poll Captions:** Perform a single non-blocking HTTP GET to http://127.0.0.1:8802/v1/meet/bridge/captions?since=<lastAt>. Do not loop or block.
3. **Analyze Speech:** Evaluate any new speech for actionable development tasks. Ignore idle chatter.
4. **Task Tracking:** Update the 	odos SQL database with any identified tasks. 
5. **Delegation:** Check if any sub-agent is active. If exactly zero mutating sub-agents are active, use the 	ask tool to delegate the highest-priority queued task to one general-purpose agent. Provide strict workspace confinement context. If an agent is active, leave tasks queued.
6. **Reporting:** Write the current task status and active agent to FACILITATOR_STATUS.md and output it in the chat interface.
7. **Exit:** Exit the turn immediately, leaving the native scheduler to initiate the next cycle.

## Conflict & Safety Rules
- **No Background Loops:** Do not use while ($true) or Start-Sleep to keep the turn alive.
- **Git Safety:** Do not mutate files outside the CO-IDE-WS. Inspect Git status before mutation.
- **Interruption Guard:** If invoked while the user is actively making a direct request in the UI, exit immediately without interfering.
