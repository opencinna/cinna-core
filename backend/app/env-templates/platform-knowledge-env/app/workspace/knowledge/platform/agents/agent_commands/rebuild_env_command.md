# `/rebuild-env` Command

## Purpose

Rebuilds the active environment for the current agent directly from the chat interface — equivalent to clicking the "Rebuild" button on the environment panel. Updates core system files and Docker image while preserving all workspace data.

## When to Use

- After a platform update that requires updated core files in the environment
- The environment needs a fresh Docker image rebuild
- The environment is in an error state that a simple restart can't fix
- You want to rebuild without switching to the environment panel

## Execution Flow

1. Validate environment exists and is in a rebuildable state (`running`, `stopped`, `error`, or `suspended`)
2. Query all sessions connected to this environment
3. Check `ActiveStreamingManager` — if any session is actively streaming, reject with error
4. Fire off `EnvironmentService.rebuild_environment()` as a **background task** (does not block the command response)
5. Return success message immediately as a system message

The actual rebuild (stop → update core → rebuild image → start) runs asynchronously. The frontend receives realtime `ENVIRONMENT_STATUS_CHANGED` events at each lifecycle transition, so the App icon and environment panel update in real time.

## Business Rules

- Command fails if any session connected to the environment has an active stream in progress
- Only environments in `running`, `stopped`, `error`, or `suspended` status can be rebuilt
- The rebuild is executed as a background task — the command returns immediately with a confirmation system message
- The rebuild performs: stop container → remove container → update core files → rebuild Docker image → start new container
- Workspace data is always preserved (scripts, files, docs, credentials, databases)
- If the environment was running before rebuild, it will be started again automatically
- After rebuild, workspace dependencies (`workspace_requirements.txt`, `workspace_system_packages.txt`) are reinstalled in the new container

## Realtime Events During Rebuild

The rebuild lifecycle emits `ENVIRONMENT_STATUS_CHANGED` events so the frontend App icon updates in real time:

| Lifecycle Phase | Status Emitted | Frontend Effect |
|----------------|---------------|-----------------|
| Rebuild starts | `rebuilding` | App icon shows spinner ("Activating...") |
| Rebuild succeeds (was running) | `ENVIRONMENT_ACTIVATED` event | App icon returns to normal |
| Rebuild succeeds (was stopped) | `stopped` | App icon returns to normal |
| Rebuild fails | `error` | App icon returns to normal |

## Behavior

| Scenario | Response | Side Effect |
|----------|----------|-------------|
| Environment running, no active streams | "Environment rebuild initiated..." | Full rebuild cycle (background), auto-starts after |
| Environment stopped | "Environment rebuild initiated..." | Full rebuild cycle (background), stays stopped |
| Environment in error state | "Environment rebuild initiated..." | Full rebuild cycle (background) |
| Environment suspended | "Environment rebuild initiated..." | Full rebuild cycle (background) |
| Active streaming in progress | Error: "Cannot rebuild environment — an active streaming session is in progress..." | None |
| Environment building/creating/etc. | Error: "Cannot rebuild environment — current status is **{status}**..." | None |
| Environment not found | Error: "Environment not found." | None |

## Integration Points

- **[Agent Environments](../agent_environments/agent_environments.md)** — Environment lifecycle (rebuild) executed by this command
- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — Sessions queried to check for active streaming
- **[Realtime Events](../../application/realtime_events/event_bus_system.md)** — `ENVIRONMENT_STATUS_CHANGED` events emitted during rebuild lifecycle; `ENVIRONMENT_ACTIVATED` emitted on successful restart

## Technical Reference

- `backend/app/services/agents/commands/rebuild_env_command.py` — Command handler (validation + background task dispatch)
- `backend/app/services/agents/commands/rebuild_env_command.py:_rebuild_environment_background()` — Background coroutine that performs the actual rebuild
- `backend/app/services/sessions/active_streaming_manager.py` — `is_any_session_streaming()` method
- `backend/app/services/environments/environment_service.py` — `rebuild_environment()` method
- `backend/app/services/environments/environment_lifecycle.py` — `rebuild_environment()` — emits `ENVIRONMENT_STATUS_CHANGED` events at start, completion, and error
- `frontend/src/routes/_layout/session/$sessionId.tsx` — Subscribes to `ENVIRONMENT_STATUS_CHANGED` to update App icon state
