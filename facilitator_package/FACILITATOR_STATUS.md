# Facilitator Status Tracking

When a background sub-agent completes a task, or when the facilitator detects new tasks during a polling cycle, it renders a Markdown status table directly in its chat reply.

The table must track:
- **Task ID / Title**: A short description of the requested action.
- **Agent Assigned**: The name of the `general-purpose` sub-agent handling the task (only one sub-agent can be active at a time).
- **Status**: The current state of the task (e.g., `Queued`, `Running`, `Done`, `Blocked`).
- **Time Since Check**: How long it has been since the facilitator last polled or updated this status.
- **Current Cycle**: The number of 10-second cycles completed in the current 50-cycle block.

*Note: The facilitator maintains this status internally in its SQL `todos` database and renders it dynamically. This file exists as documentation of the tracking requirement.*