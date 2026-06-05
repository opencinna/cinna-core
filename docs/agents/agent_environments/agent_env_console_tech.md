# Agent Environment Console — Technical Details

## File Locations

### env-core (inside container)

- `backend/app/env-templates/app_core_base/core/server/routes.py` — `WS /shell/pty` endpoint (lines ~2187+). Ships to containers only on **rebuild**. Handles AGENT_AUTH_TOKEN verification, preamble parsing, PTY spawn, bidirectional pumps, resize, idle watchdog, SIGTERM→SIGKILL teardown.

### Backend — Services

- `backend/app/services/environments/environment_console_service.py` — `EnvironmentConsoleService`: `run_terminal_tunnel`, `follow_logs`, `_pump_bidirectional`, `_token_still_valid`, `_audit_async`. Full lifecycle owner for both consoles.
- `backend/app/services/environments/env_console_activity_tracker.py` — `EnvConsoleActivityTracker` module-level singleton (`env_console_activity_tracker`): per-connection keep-warm registry, per-user open-rate sliding window, suspension scheduler skip gate.
- `backend/app/services/environments/agent_env_connector.py` — `AgentEnvConnector.open_shell_websocket()`: opens WS to env-core `/shell/pty` with `AGENT_AUTH_TOKEN`, sends optional preamble JSON frame. `max_size=None` for PTY burst output.

### Backend — Routes

- `backend/app/api/routes/environments.py` — `WS /{id}/terminal` and `WS /{id}/logs/stream` thin controllers on a dedicated `console_ws_router` (prefix `/env-console`, registered separately in `api/main.py`); `_ws_source_ip()` helper for audit. The separate prefix lets the reverse proxy enable WebSocket upgrade with one prefix `location` block without touching the REST `/environments` endpoints — see [Nginx Setup](../../infrastructure/nginx_setup.md).

### Backend — Dependencies

- `backend/app/api/deps.py` — `EnvConsoleContext` dataclass, `get_env_console_context_ws(require_terminal)` factory, `_resolve_platform_user_from_token()` shared token validator (used by both the WS dep and `EnvironmentConsoleService._token_still_valid`), `_extract_ws_token()` (accepts `?token=` or `Authorization` header).

### Backend — Docker Adapter

- `backend/app/services/environments/adapters/docker_adapter.py` — `get_logs(lines, follow=True)` returns an async generator backed by a worker thread (`asyncio.to_thread`) that drains the blocking Docker SDK log iterator into an `asyncio.Queue(maxsize=1000)`. `_follow_logs()` internal method; `_empty_async_iter()` fallback.

### Backend — Suspension Scheduler

- `backend/app/services/environments/environment_suspension_scheduler.py` — Imports `env_console_activity_tracker` and calls `is_console_warm(env.id)` as a skip gate before the inactivity check.

### Backend — Config

- `backend/app/core/config.py` — All console-related settings:
  - `ENV_TERMINAL_IDLE_TIMEOUT_SECONDS: int = 900`
  - `ENV_CONSOLE_MAX_PER_ENV: int = 3`
  - `ENV_CONSOLE_MAX_PER_USER: int = 10`
  - `ENV_CONSOLE_OPEN_RATE_LIMIT: int = 10`
  - `ENV_CONSOLE_OPEN_RATE_WINDOW_SECONDS: int = 60`
  - `ENV_CONSOLE_LOGS_TAIL_DEFAULT: int = 200`
  - `ENV_CONSOLE_LOGS_TAIL_MAX: int = 5000`

### Backend — Security Events

- `backend/app/models/events/security_event.py` — String constants `AGENT_ENV_TERMINAL_OPENED` and `AGENT_ENV_TERMINAL_CLOSED` defined at module level (no DB schema change — `event_type` is a free-form string column).

### Frontend — Components

- `frontend/src/components/Environments/EnvironmentConsoleDrawer.tsx` — shadcn `Sheet` hosting the console; two modes selected by `kind: EnvConsoleKind`. Seeds logs from REST snapshot before WS attaches. Sensitivity notice banner for terminal. Jump-to-bottom for logs. Reconnect/Close footer controls.
- `frontend/src/components/Environments/XtermConsole.tsx` — Thin React wrapper around xterm.js. Mounts `Terminal` + `FitAddon` + `WebLinksAddon` into a ref'd `<div>`, `ResizeObserver` for reflow. `readOnly` prop disables stdin and hides cursor (logs mode). Imperative handle (`XtermConsoleHandle`) with `write`, `clear`, `focus`, `scrollToBottom`, `getSize`.
- `frontend/src/components/Environments/EnvironmentCard.tsx` — Props `canFollowLogs`, `canOpenTerminal`, `onOpenConsole`. `Logs` button visible when `!readOnly && canFollowLogs`; `Terminal` button visible when `!readOnly && canOpenTerminal`, enabled only when `status == "running"`.
- `frontend/src/components/Agents/AgentEnvironmentsTab.tsx` — Hosts the shared `EnvironmentConsoleDrawer` instance; computes `canFollowLogs = isDeveloper && isOwner` and `canOpenTerminal = isDeveloper && isOwner`; passes `onOpenConsole` callback to each `EnvironmentCard`.

### Frontend — Hooks

- `frontend/src/hooks/useEnvConsoleSocket.ts` — Raw `WebSocket` client (the generated OpenAPI client does not cover WS). Builds WS URL from `OpenAPI.BASE`, appends `?token=<access_token>` from `localStorage`. `binaryType = "arraybuffer"`. Parses `{"type":"closed"}` in-band close sentinel. Capped exponential backoff (5 attempts, max 8 s). Non-retryable close codes: `4404`, `4429`, `1008`, `1011`, `1000`, `1001`.

### Frontend — Dependencies

- `frontend/package.json` — `@xterm/xterm`, `@xterm/addon-fit`, `@xterm/addon-web-links`.

## API Endpoints

### Browser-Facing WebSocket Routes

These are **WebSocket** routes mounted under the dedicated `/env-console` prefix (not `/environments`) so the reverse proxy can scope WebSocket-upgrade to a single prefix block. They do not appear in the generated OpenAPI client.

| Endpoint | Auth | Access | Close codes |
|----------|------|--------|-------------|
| `WS /api/v1/env-console/{id}/terminal` | `?token=<platform JWT>` | Owner + agent-developer/superuser | 1008 bad token/perm, 4404 not running, 4429 cap, 1011 internal |
| `WS /api/v1/env-console/{id}/logs/stream?tail=N` | `?token=<platform JWT>` | Owner or superuser | 1008 bad token/perm, 4404 not running, 4429 cap |

### env-core Internal WebSocket (Terminal Only)

| Endpoint | Auth | Caller |
|----------|------|--------|
| `WS /shell/pty` | `Authorization: Bearer {AGENT_AUTH_TOKEN}` | Backend proxy only |

Accessed over the internal Docker bridge network (`agent-bridge`). Never reachable from the browser directly.

### REST Snapshot (Logs Seed)

| Endpoint | Auth | Notes |
|----------|------|-------|
| `GET /api/v1/environments/{id}/logs?lines=N` | Platform JWT (HTTP) | Existing endpoint; used by the drawer to seed an instant first paint before WS attaches |

## WS Wire Contract

### Terminal

- **Browser → backend**: binary frames = raw PTY keystrokes; text frames = JSON control.
  - Resize control: `{"type":"resize","cols":N,"rows":M}` — cols/rows clamped to 1–500 by env-core.
- **Backend → browser**: binary frames = raw PTY screen bytes (decoded UTF-8 by `TextDecoder` in the frontend).
- **Server close**: `{"type":"closed","reason":"idle_timeout"}` text frame before the WS close, when the env-core idle watchdog fires.

### Logs

- **Browser → backend**: text frames = optional control (currently accepted and drained but not yet acted on: `{"type":"set_tail"}`, `{"type":"pause"}`).
- **Backend → browser**: text frames = log lines (one `\n`-terminated line per frame).
- **Server close**: `{"type":"closed"}` text frame when the container stops (followed by WS close from the backend).

## Service Layer Details

### EnvironmentConsoleService (`backend/app/services/environments/environment_console_service.py`)

All methods are class methods. The service holds no per-instance state; all runtime state lives on `env_console_activity_tracker`.

#### `run_terminal_tunnel(websocket, environment, agent_id, user, raw_token, source_ip)`

1. Status guard: rejects with close `4404` if `environment.status != "running"`.
2. Open-rate cap (`enforce_open_rate`) and pre-register concurrency cap (`_enforce_concurrency_pre`) — both raise before `websocket.accept()`.
3. `websocket.accept()`, register connection, TOCTOU re-check of concurrency cap post-register.
4. Audit `AGENT_ENV_TERMINAL_OPENED` SecurityEvent.
5. Resolve env URL + auth headers via `MessageService.get_environment_url` / `get_auth_headers`.
6. `agent_env_connector.open_shell_websocket(base_url, auth_headers, preamble={"cols":80,"rows":24,"shell":"bash"})`.
7. `_pump_bidirectional`: three concurrent tasks (`client_to_env`, `env_to_client`, `heartbeat_loop`); `asyncio.wait(FIRST_COMPLETED)` cancels survivors on any task finish.
8. Heartbeat: 30 s tick, calls `_token_still_valid(raw_token)`; token failure sets `exit_reason = "token_expired"` and returns (triggers teardown).
9. Teardown: unregister tracker, audit `AGENT_ENV_TERMINAL_CLOSED` with `duration_seconds` and `exit_reason`.

#### `follow_logs(websocket, environment, user, raw_token, tail)`

1. Status + rate + concurrency guards (same as terminal).
2. `websocket.accept()`, register, TOCTOU check.
3. Three concurrent tasks: `log_forwarder` (streams `adapter.get_logs(lines=tail, follow=True)`), `control_reader` (drains client text frames), `heartbeat_loop`.
4. On any task completion: cancel all, unregister tracker, send `{"type":"closed"}` text frame if WS still open, close WS.

#### `_pump_bidirectional(...)`

Shared helper used by the terminal path. Runs two pump tasks plus a heartbeat with `asyncio.wait(FIRST_COMPLETED)`; cancels all survivors and closes `env_ws` in `finally`.

#### `_token_still_valid(raw_token)`

Imports `_resolve_platform_user_from_token` lazily and opens its own `Session(engine)`. Returns `False` on any exception (expired JWT, revoked desktop token, inactive user).

### EnvConsoleActivityTracker (`backend/app/services/environments/env_console_activity_tracker.py`)

Module-level singleton `env_console_activity_tracker`.

| Method | Purpose |
|--------|---------|
| `register_connection(env_id, conn_id)` | Add conn_id to the env's set; stamp `last_activity_at`. |
| `unregister_connection(env_id, conn_id)` | Remove conn_id; stamp `last_activity_at` (starts the grace period clock). |
| `heartbeat(env_id)` | Stamp `last_activity_at` if the env is still warm. |
| `is_console_warm(env_id)` | `True` if `≥1` connection registered. Used by suspension scheduler. |
| `count_for_env(env_id)` | Concurrency cap check. |
| `count_for_user(set[env_id])` | Per-user cap check; caller bounds the set to `attached_env_ids()` for efficiency. |
| `attached_env_ids()` | Env-ids with ≥1 connection; used to bound the per-user ownership DB query. |
| `enforce_open_rate(user_id, limit, window)` | Sliding-window open-rate cap; raises `ConsoleRateLimitError`. |
| `reset()` | Test isolation only; clears all state. |

Each `_update_env_activity` opens its own `Session(engine)` (does not take a dep-injected session — same pattern as `SyncActivityTracker`).

### AgentEnvConnector.open_shell_websocket

```python
async def open_shell_websocket(
    self,
    base_url: str,
    auth_headers: dict,
    preamble: dict | None = None,
) -> websockets.WebSocketClientProtocol
```

- Converts `http://` → `ws://`, appends `/shell/pty`.
- `websockets.connect(..., max_size=None)` (PTY TUI redraws can be large).
- Sends `json.dumps(preamble)` as a text frame if provided; closes and re-raises on failure.

### Docker Adapter `get_logs(follow=True)`

Returns `AsyncIterator[str]`. Internal `_follow_logs` method:

1. Calls `container.logs(stream=True, follow=True, tail=lines)` — returns a blocking socket iterator.
2. Spawns `asyncio.to_thread(_drain_blocking)` where `_drain_blocking` iterates the socket and pushes decoded lines to `asyncio.Queue(maxsize=1000)` via `asyncio.run_coroutine_threadsafe`.
3. The async generator yields from the queue. `None` sentinel signals end-of-stream.
4. On generator cancellation: sets `stop = True`, calls `log_iter.close()` to unblock the worker thread, awaits the drain task.

## Frontend Component Details

### XtermConsole (`XtermConsoleHandle`)

| Handle method | Description |
|---------------|-------------|
| `write(data: string \| Uint8Array)` | Write a chunk to the terminal screen |
| `clear()` | Clear scrollback + viewport |
| `focus()` | Focus terminal for keystroke capture |
| `scrollToBottom()` | Jump logs view to bottom |
| `getSize()` | Current `{cols, rows}` after last fit |

Configuration: dark theme (`background: "#0b0f17"`), 5 000-line scrollback, 13 px monospace, `convertEol: true`. `readOnly` disables stdin and hides the cursor (invisble cursor color = background).

`ResizeObserver` + `requestAnimationFrame` coalescing prevents layout thrash on rapid resizes. `FitAddon.fit()` is called once per animation frame at most.

### useEnvConsoleSocket

URL construction: `OpenAPI.BASE` (= `VITE_API_URL` if set, else `window.location.origin`) is converted `http(s) → ws(s)`, then the appropriate path and `?token=` + `?tail=` params are appended.

Non-retryable close codes that suppress the backoff reconnect loop:

| Code | Meaning |
|------|---------|
| `4404` | Environment not running |
| `4429` | Concurrency / rate cap exceeded |
| `1008` | Unauthorized / policy violation |
| `1011` | Internal error (env-core unreachable / pre-rebuild env has no shell endpoint) |
| `1000` | Normal close (drawer closed by user) |
| `1001` | Going away (page unload) |

In-band sentinel parsing: `parseClosedSentinel(text)` fast-bails if the frame does not contain `"closed"`, then parses JSON and maps `reason: "idle_timeout"` → `EnvConsoleCloseReason("idle_timeout")` or else → `"container_stopped"`.

### EnvironmentConsoleDrawer — Logs Seed

`useQuery(["environment-logs-snapshot", id, 500])` calls `EnvironmentsService.getEnvironmentLogs({id, lines: 500})`. Painted once via `consoleRef.current.write()` only if the live WS has not yet delivered a frame (`liveStartedRef.current === false`). This prevents stale tail lines painting after live output.

### AgentEnvironmentsTab — Drawer Ownership

The single `EnvironmentConsoleDrawer` instance is controlled by `consoleTarget: { environment, kind } | null` state. Opening a console on any card sets `consoleTarget`; the drawer `open={!!consoleTarget}` pattern means only one console is open at a time.

## env-core Shell PTY Endpoint (`WS /shell/pty`)

Located in `backend/app/env-templates/app_core_base/core/server/routes.py`.

| Phase | Detail |
|-------|--------|
| **Auth** | Checks `Authorization: Bearer {AGENT_AUTH_TOKEN}` before `accept()`. Close `1008` on mismatch. |
| **Preamble** | Awaits first frame with 5 s timeout. Text JSON `{"cols":N,"rows":M,"shell":"bash"}` sets initial PTY size and shell. Binary first frame or timeout: uses defaults (80×24, bash). |
| **Shell resolution** | `shutil.which(requested_shell)` → `bash` fallback → `sh` fallback. If none available: send `{"type":"error","message":"no shell available"}` + close `1011`. |
| **PTY spawn** | `pty.openpty()` → master/slave fds. `_set_pty_winsize(master_fd, rows, cols)` via `TIOCSWINSZ`. `asyncio.create_subprocess_exec(shell, "-i", stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, cwd="/app/workspace", start_new_session=True)`. |
| **pty→ws pump** | `loop.add_reader(master_fd, ...)` — non-blocking read via event loop. Bytes sent as WS binary frames. |
| **ws→pty pump** | Binary frames → `os.write(master_fd, data)`. Text frames → JSON decode → `{"type":"resize","cols":N,"rows":M}` → `_set_pty_winsize`. Cols/rows clamped to 1–500. |
| **Idle watchdog** | Tracks `last_input_at`; closes (SIGTERM→SIGKILL) if no inbound bytes for `ENV_TERMINAL_IDLE_TIMEOUT_SECONDS`. |
| **Teardown** | Either side disconnects or process exits: SIGTERM the process group (`os.killpg(os.getpgid(proc.pid), signal.SIGTERM)`), wait 2 s, SIGKILL. `os.close(master_fd)`. Send `{"type":"closed","reason":"idle_timeout"}` for idle closes. Close WS. |

## Security Details

### Scoped Token Defense-in-Depth

`_resolve_platform_user_from_token` explicitly rejects tokens where `payload["token_type"] in {"guest_share","webapp_share"}` or `payload["role"] in {"chat-guest","webapp-viewer"}`. This is defense-in-depth on top of the ownership check: scoped tokens carry a share UUID in `sub`, not a user UUID, so the ownership check would fail anyway — but the explicit rejection prevents any future code path that might look up by share id from accidentally granting console access.

### Desktop Token Revocation

The `_resolve_platform_user_from_token` function (and therefore the heartbeat re-check) calls `DesktopAuthService.verify_active_or_raise` for tokens with `client_kind == "desktop"`, ensuring a revoked desktop token tears down the shell socket on the next 30 s heartbeat tick.

### TOCTOU Concurrency Cap

The two-phase check (pre-register: `>=` threshold rejects; post-register: `>` threshold rejects) prevents N simultaneous opens that all pass the pre-check from collectively exceeding the cap. The loser(s) unregister and close with `4429`.

## Configuration Reference

All settings are in `backend/app/core/config.py` (Pydantic Settings):

| Setting | Default | Description |
|---------|---------|-------------|
| `ENV_TERMINAL_IDLE_TIMEOUT_SECONDS` | `900` | Env-core idle watchdog timeout in seconds |
| `ENV_CONSOLE_MAX_PER_ENV` | `3` | Max concurrent consoles per environment |
| `ENV_CONSOLE_MAX_PER_USER` | `10` | Max concurrent consoles per user (across all their envs) |
| `ENV_CONSOLE_OPEN_RATE_LIMIT` | `10` | Max console opens per user per sliding window |
| `ENV_CONSOLE_OPEN_RATE_WINDOW_SECONDS` | `60` | Sliding window duration in seconds |
| `ENV_CONSOLE_LOGS_TAIL_DEFAULT` | `200` | Default tail size for `WS /logs/stream` |
| `ENV_CONSOLE_LOGS_TAIL_MAX` | `5000` | Maximum tail size (server clamps any higher value) |

These settings can be overridden via environment variables (`.env` file or Docker compose) using the same name.

## No Database Migration

No Alembic migration is required. `SecurityEvent.event_type` is a free-form `VARCHAR` column (not a Postgres `ENUM`), so `AGENT_ENV_TERMINAL_OPENED` and `AGENT_ENV_TERMINAL_CLOSED` are code-only string constants with no schema change.

`EnvConsoleActivityTracker` is a module-level in-memory singleton with no database table.

## WebSocket Events (Existing, Unchanged)

The console does not introduce new platform-level WebSocket events (Socket.IO). Environment status changes that affect console availability (e.g., `ENVIRONMENT_SUSPENDED`) continue to arrive via the existing event bus and are reflected in the environment list poll that gates the console buttons.
