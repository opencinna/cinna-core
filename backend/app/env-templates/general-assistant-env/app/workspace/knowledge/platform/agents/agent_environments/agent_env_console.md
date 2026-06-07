# Agent Environment Console

## Purpose

Give agent developers two operator-facing windows into a running environment: live container log tailing and a full interactive web terminal (PTY shell). Both are surfaced per-card on the Environments tab as `Logs` and `Terminal` buttons and open in a right-side drawer with an xterm.js console.

## Core Concepts

### Two Distinct Capabilities

| Capability | What it does | Where it runs | Who can use it |
|-----------|--------------|----------------|----------------|
| **Logs follow** | Streams the container's stdout/stderr (`docker logs -f`) in real time with a tail snapshot on open | **Host-side** via Docker adapter — no env-core change required | Owner or superuser (any role) |
| **Web terminal (PTY)** | Opens an interactive `bash -i` shell inside the container at `/app/workspace` with full PTY support (color, arrow keys, TUI apps, resize) | **Inside the container** via env-core `WS /shell/pty` — requires an environment rebuild to ship | Owner + `agent-developer` or superuser only |

The **key asymmetry**: logs work immediately on any existing environment because they are read from the Docker daemon on the host. The terminal requires the container to be rebuilt first because the `WS /shell/pty` endpoint only arrives in the container's `app/core/` on rebuild. The drawer surfaces a user-facing note when the terminal connection fails ("rebuild to enable the terminal").

### 3-Layer WS Topology (Terminal)

```
Browser          Backend                    env-core (in container)
xterm.js  ──WS──►  proxy  ──WS (Bearer)──►  /shell/pty
         ◄──────   route  ◄────────────────  bash -i PTY
         ?token=JWT       AGENT_AUTH_TOKEN
```

For **logs**, the backend route streams directly from the Docker adapter — env-core is not in the path.

This topology mirrors the [Cinna CLI Integration](../../application/cinna_cli_integration/cinna_cli_integration.md) sync tunnel: same WS proxy pattern, same keep-warm signal, same per-tick token revocation re-check.

## User Flows

### 1. Follow Container Logs

1. Developer opens an agent → **Environments** tab → finds the target environment card.
2. Clicks **Logs** (enabled whenever the environment is `running`).
3. A right-side drawer opens with the xterm console. A tail snapshot (up to 500 lines from REST `GET /environments/{id}/logs`) paints instantly while the live WS attaches.
4. Live log lines stream in real time. The console auto-scrolls; scrolling up locks autoscroll and shows a **Jump to bottom** button.
5. Closing the drawer terminates the WS and the host-side follow generator stops.

### 2. Open an Interactive Terminal

1. Developer opens the agent → **Environments** tab → finds a **running** environment card.
2. Clicks **Terminal** (only visible for owners with the `agent-developer` or `admin` role; disabled unless `status == running`).
3. A right-side drawer opens with a sensitivity notice: "Full shell access — commands run as the agent and can read its credentials. Activity is audited."
4. The backend opens a WS to env-core `WS /shell/pty`, which spawns `bash -i` (or `sh` as fallback) at `cwd=/app/workspace`.
5. The shell prompt appears. The developer can run arbitrary commands, launch TUI apps (vim, top), navigate the filesystem, inspect credentials.
6. Resizing the drawer reflows the terminal grid; a resize control frame is sent to the backend PTY.
7. Closing the drawer (or leaving the browser idle) terminates the shell process group.

### 3. Reconnect After a Drop

- If the environment stops mid-session, the drawer shows a red banner explaining why (e.g., "The environment is not running. Start it, then reconnect.").
- Transient drops auto-reconnect with capped exponential backoff (up to 5 attempts, max 8 s delay).
- Non-transient closes (environment not running, concurrency cap exceeded, unauthorized, internal error on pre-rebuild env) suppress auto-retry and show a **Reconnect** button.

## Business Rules

### Access Control

| Capability | Backend gate | Frontend visibility |
|-----------|--------------|---------------------|
| Terminal | Owner of the environment AND (`agent-developer` role OR `is_superuser`) | Button only in `isDeveloper && isOwner` context; never in `readOnly` or agent-user cards |
| Logs | Owner of the environment OR `is_superuser` | Button only in `isDeveloper && isOwner` context; never in `readOnly` or guest views |

The server-side WebSocket dep (`get_env_console_context_ws`) is the security boundary. The frontend gate is UX-only. Non-owner users, agent-users, and guests receive close code `1008` on the WS handshake and never reach a shell or log stream.

Scoped tokens (`guest_share`, `webapp_share`) are explicitly rejected at the token-decode step — they cannot be promoted to a console session even if their `sub` claim would match.

### Status Guard

Both consoles reject connections when the environment is not `running`, closing the socket with code `4404`. A suspended or stopped environment never gets auto-started by opening a console (no surprise resource spin-up). The drawer shows a "Start the environment to open a console" CTA.

### Keep-Warm / Suspension Gate

Opening any console (terminal or logs) registers the connection with `EnvConsoleActivityTracker` and stamps `last_activity_at` on the environment. While at least one console is attached, the suspension scheduler skips the environment (mirrors the CLI sync keep-warm gate). Normal inactivity rules resume after the last console detaches.

### Concurrency and Rate Caps

| Cap | Default | Config key |
|-----|---------|------------|
| Max consoles per environment | 3 | `ENV_CONSOLE_MAX_PER_ENV` |
| Max consoles per user (across all their envs) | 10 | `ENV_CONSOLE_MAX_PER_USER` |
| Max console opens per user per minute (sliding window) | 10 opens / 60 s | `ENV_CONSOLE_OPEN_RATE_LIMIT` / `_WINDOW_SECONDS` |

Exceeding any cap closes the socket with code `4429`; the drawer shows a user-readable reason.

### Idle Timeout (Terminal)

A terminal with no inbound keystrokes for `ENV_TERMINAL_IDLE_TIMEOUT_SECONDS` (default 900 s / 15 min) is closed by the env-core idle watchdog. An in-band `{"type":"closed","reason":"idle_timeout"}` text frame arrives before the close, and the drawer shows a user-readable banner.

### Logs Tail

Initial tail snapshot is clamped to at most `ENV_CONSOLE_LOGS_TAIL_MAX` (default 5 000) lines. The default tail for the REST seed call is 500 lines; the WS `?tail=` param defaults to 200.

### Heartbeat and Token Re-check

Both consoles run a 30-second heartbeat loop that re-validates the platform JWT with a fresh database session. An expired or revoked token (including desktop-token revocation) tears down the socket on the next heartbeat tick, mirroring the CLI sync tunnel behavior.

## Audit Trail

Every terminal open and close writes a `SecurityEvent` row:

| Event type | Severity | Details fields |
|-----------|----------|----------------|
| `AGENT_ENV_TERMINAL_OPENED` | medium | `env_id`, `agent_id`, `user_id`, `source_ip` |
| `AGENT_ENV_TERMINAL_CLOSED` | medium | `env_id`, `agent_id`, `user_id`, `source_ip`, `duration_seconds`, `exit_reason` |

Logs follow is not audited (read-only, lower sensitivity). Superusers can review terminal audit events in the Security Events table. This mirrors the [Admin Agent Environments](../../application/admin_agent_environments/admin_agent_environments.md) SecurityEvent pattern.

## Security Considerations

A full interactive shell inside the container can read `/app/workspace/credentials/` (the agent's decrypted credential files) and any AI provider environment variables injected at container start. This is why the terminal is gated to owner + developer/superuser:

- The shell runs with the **same OS user and filesystem permissions** as the env-core process — no privilege escalation.
- Credential files are visible as they are to the agent itself.
- Every session is audited with user identity and source IP.
- No terminal surface is reachable through guest shares, A2A, MCP, or agent-user–simplified views.
- The drawer header shows a permanent one-line sensitivity notice so the developer is never surprised.

## Integration Points

- **[Agent Environments](./agent_environments.md)** — The Environments tab hosts the console drawer. Environment status (`running` / not running) gates console availability. Suspension criteria now include `is_console_warm` (see Suspension Criteria section).
- **[User Roles](../../application/user_roles/user_roles.md)** — Terminal access is gated on `agent-developer`/`admin` role. `agent-user` accounts are silently excluded (button never rendered; backend returns 1008).
- **[Cinna CLI Integration](../../application/cinna_cli_integration/cinna_cli_integration.md)** — The terminal WS topology mirrors the CLI sync tunnel (`run_sync_tunnel` → `run_terminal_tunnel`); `EnvConsoleActivityTracker` mirrors `SyncActivityTracker`; both provide keep-warm gates for the suspension scheduler.
- **[Admin Agent Environments](../../application/admin_agent_environments/admin_agent_environments.md)** — Shares the SecurityEvent audit infrastructure; console terminal events use the same severity and record format as admin rebuild events.
- **[Agent Environment Core](../agent_environment_core/agent_environment_core.md)** — The terminal endpoint `WS /shell/pty` lives inside `app_core_base/core/server/routes.py` and ships to the container only on rebuild. The PTY shell runs inside the same process space as env-core.

## Future Enhancements (Out of Scope)

- Terminal session recording/replay (asciinema-style) for audit.
- Read-only shared terminal view for collaborators (multi-attach to one PTY).
- Logs search, filter, and download buffer.
- Admin fleet console from `/admin/agent-envs` reusing `EnvironmentConsoleService`.
- Multiplexed tabs (multiple shells per env).
- Surfacing the console inside the session env-panel widget.
