# ACP integration — technical reference

## File locations

| Layer | Files |
|---|---|
| Connector models | `backend/app/models/acp/acp_connector.py`, `backend/app/models/acp/acp_token.py` |
| Management API | `backend/app/api/routes/acp_connectors.py`, registered in `backend/app/api/main.py` |
| Management and authentication | `backend/app/services/acp/connector_service.py` |
| ACP transport | `backend/app/acp/server.py`, registered and shut down by `backend/app/main.py` |
| Protocol/session adapter | `backend/app/acp/agent.py` |
| Shared execution | `backend/app/services/sessions/stream_processor.py`, `backend/app/services/sessions/message_service.py`, `backend/app/services/sessions/session_service.py` |
| Migration | `backend/app/alembic/versions/6bdc1a2e709f_add_acp_connectors_and_tokens.py` |
| UI | `frontend/src/components/Agents/Acp/`, hosted in `frontend/src/components/Agents/AgentIntegrationsTab.tsx` |
| Generated client | `frontend/src/client/sdk.gen.ts`, `frontend/src/client/types.gen.ts`, `frontend/src/client/schemas.gen.ts` |
| External clients | `backend/clients/acp/client.py`, `backend/clients/acp/stdio_bridge.py` |
| Tests | `backend/tests/api/acp_integration/test_acp_connectors.py`, `backend/tests/api/acp_integration/test_acp_runtime.py`, `backend/tests/utils/acp.py`, `backend/tests/utils/acp_runtime.py`, `backend/tests/unit/test_acp_stdio_bridge.py`, `backend/tests/migrations/acp_connectors_test.py`, `frontend/tests/acpConnectors.test.mjs` |

The implementation pins `agent-client-protocol[http]==0.12.1` in `backend/pyproject.toml` and `backend/uv.lock`. Its `Connection`, schema models, and JSON-RPC error machinery are reused; `WebSocketTransport` supplies the Cinna authentication and resource limits.

## Database schema

### ACPConnector (`acp_connector`)

- `id`: UUID primary key.
- `agent_id`, `owner_id`: indexed UUID foreign keys to agent/user with cascade deletion.
- `name`: trimmed nonempty string, at most 255 characters.
- `mode`: conversation/building; validated in input schemas and constrained in the database.
- `is_active`: boolean, defaults true.
- `max_connections`: integer 1–100, defaults ten, with database constraint.
- `created_at`, `updated_at`: timezone-aware UTC timestamps.

`ACPConnectorPublic` adds `acp_server_url`. The list envelope contains `data` and `count`. Updates reject explicit nulls for nonnullable fields.

### ACPToken (`acp_token`)

- `id`: UUID primary key; `connector_id`: indexed foreign key with cascade deletion.
- `token_hash`: unique indexed SHA-256 digest. The secret is generated from 48 cryptographically random bytes and prefixed `acp_`.
- `prefix`: first twelve characters for identification; `label`: trimmed nonempty string, at most 255 characters.
- `revoked`: boolean; permanent revocation has no restore operation.
- `expires_at`, `created_at`, `last_used_at`: timezone-aware timestamps; last-used is nullable and records accepted connections.

`ACPTokenCreate` accepts `label` and `expires_in_days` (1–365, default 90). `ACPTokenCreated` includes the full `token` once. `ACPTokenPublic` contains lifecycle metadata but omits both token and hash. The create response carries `Cache-Control: no-store` and `Pragma: no-cache`.

### Persistent session binding

No session-table migration is required. `backend/app/models/sessions/session.py` stores `integration_type="acp"`, the owner's `user_id`, agent ID, and connector mode. `session_metadata["acp"]` is a map containing string `connector_id`, string `token_id`, and `cwd="/app/workspace"`.

The canonical lowercase, hyphenated platform session UUID is the ACP session ID; alternative UUID spellings are rejected before locks or admission checks. Access requires an exact metadata match plus integration type, owner, agent, and current connector-mode checks. Deleting tokens/connectors leaves historical sessions intact but removes their authorization path. Rotating a token creates a new session scope.

## API endpoints

All management routes use `CurrentUser` and `SessionDep`, enforce exact agent ownership, and validate nested connector/token IDs. The router tag generates `AcpConnectorsService`.

| Method | Path | Request / result |
|---|---|---|
| POST | `/api/v1/agents/{agent_id}/acp-connectors` | name, mode?, is_active?, max_connections? → ACPConnectorPublic |
| GET | `/api/v1/agents/{agent_id}/acp-connectors` | → ACPConnectorsPublic |
| GET | `/api/v1/agents/{agent_id}/acp-connectors/{connector_id}` | → ACPConnectorPublic |
| PUT | `/api/v1/agents/{agent_id}/acp-connectors/{connector_id}` | name?, mode?, is_active?, max_connections? → ACPConnectorPublic |
| DELETE | `/api/v1/agents/{agent_id}/acp-connectors/{connector_id}` | → confirmation message |
| POST | `/api/v1/agents/{agent_id}/acp-connectors/{connector_id}/tokens` | label, expires_in_days? → ACPTokenCreated |
| GET | `/api/v1/agents/{agent_id}/acp-connectors/{connector_id}/tokens` | → ACPTokensPublic |
| POST | `/api/v1/agents/{agent_id}/acp-connectors/{connector_id}/tokens/{token_id}/revoke` | → ACPTokenPublic |
| DELETE | `/api/v1/agents/{agent_id}/acp-connectors/{connector_id}/tokens/{token_id}` | → confirmation message |

Missing or foreign agent/connector/token IDs return 404. Building-mode creation or updates by an owner lacking developer privileges return 403. Invalid input returns 422. Token authentication is only available on the ACP transport; an ACP bearer token does not authenticate management REST calls.

The remote endpoint is the WebSocket route `/acp/{connector_id}` on the backend app, outside `/api/v1`. It accepts individual UTF-8 JSON-RPC text frames. It is not a REST POST endpoint, MCP endpoint, or Socket.IO channel.

## Services and protocol methods

`ACPConnectorService` owns connector persistence, token creation, public endpoint projection, and authentication. `authenticate(connector_id, raw_token, db)` joins connector/token/agent/owner, checks active state, expiry/revocation, agent ownership, and developer role for building mode. `mark_used` separately records successful connection admission.

`CinnaACPAgent` is instantiated per authenticated connection:

| ACP operation | Cinna behavior |
|---|---|
| initialize | Validates protocol request and reports v1, agent info, load-session support, text-only capabilities, no protocol login methods, and `_meta.cinna` hosted-workspace information |
| session/new | Requires cwd `/app/workspace`, no client MCP servers/additional directories; creates and binds a Cinna session |
| session/load | Authorizes existing binding, obtains session admission, replays stored user/agent text, then completes the result |
| session/prompt | Requires a session created/loaded on this connection; validates text and setup gate; persists a user message and invokes SessionStreamProcessor |
| session/cancel | Notification only; authorizes session access and interrupts the active turn |

Unsupported methods return method-not-found. Loading/prompting an unrelated session returns not-found without revealing its history. Initialization must precede session methods. Calling cancel as a request is invalid.

Prompt execution uses `SessionStreamProcessor` and the existing environment activation and `MessageService.stream_message_with_events` pipeline. It holds the shared in-process session lock and a PostgreSQL advisory session lease to serialize ACP operations. ACP text prompts enter directly through `MessageService.create_message`; they do not invoke the web UI slash-command parser or client-side tools.

`ACPStreamEventHandler` converts assistant/thinking/tool activity into ACP session updates. Prompt completion returns `end_turn` or `cancelled`; execution failures become protocol errors. Cancellation and connection cleanup forward interruption to the real environment through `MessageService.interrupt_stream`, allow one second for normal completion, then cancel stalled local work. Forced cleanup preserves partial agent text and events and clears the streaming marker. Pending messages stopped before dispatch are marked as failed rather than left to execute on a later turn.

## Authentication and limits

A WebSocket upgrade requires `Authorization: Bearer <ACP token>`. Query parameters are rejected, so tokens cannot be passed in the URL. Native clients may omit Origin; supplied browser origins must match Cinna's configured frontend/CORS origins. Browser WebSocket APIs that cannot set Authorization need a trusted native/server bridge; query tokens and cookie authentication are not provided.

Authorization is rechecked before every per-operation entry point — `_dispatch`, `new_session`, `load_session`, `prompt`, `session/cancel`, and the watchdog (`watch_prompt`) — with an uncached query every time; these run once per operation and are cheap. Forwarding a streamed session update (`update()`, called per assistant/thought/tool event and again per 8192-character outgoing sub-chunk) instead reuses a per-session authorization result memoized for `AUTH_MEMO_TTL = 1.0` second on `CinnaACPAgent.auth_checked_at`, because a large replay can otherwise issue thousands of blocking authorization queries on the single event loop while holding its admitted prompt's pinned pooled connection. `interrupt()` pops the memo immediately so no notification made after the server decides to stop a turn is authorized from a stale entry, and it is cleared on prompt/replay release and connection cleanup.

The guarantee this leaves is precise, not instantaneous: `watch_prompt` rechecks authorization uncached every `AUTH_CHECK_INTERVAL = 5` seconds and interrupts the turn as soon as a check fails, so **a turn is interrupted within 5 seconds of the server detecting revocation, and no session update is sent after that detection** — not that the very next streamed chunk after a client revokes is blocked (the 1s memo can still authorize one chunk from a check made just before revocation). Revocation, expiry, connector disablement, or owner/agent deactivation surface as a `RequestError` from the authorization query and always stop the turn on the first occurrence — that check is never relaxed. An *unexpected* exception from the watchdog's check (a pool checkout timeout, a dropped connection) is different: it is infrastructure, not an authorization failure, so the watchdog tolerates up to `MAX_AUTH_CHECK_FAILURES - 1 = 2` consecutive unexpected failures (logging each), resets the counter on the next successful check, and only stops the turn with an internal error on the 3rd consecutive one.

| Limit | Value |
|---|---|
| Connections per worker | 64 |
| Connections per connector per worker | connector.max_connections, 1–100 |
| In-flight requests per connection | 8 |
| Active prompt plus replay admission per worker | `MAX_ACTIVE_PROMPTS = DB_POOL_SIZE // 3` — 6 at the default `DB_POOL_SIZE=20`, 2 at the minimum `DB_POOL_SIZE=8` |
| Active prompt plus replay admission per connector | `MAX_ACTIVE_PROMPTS_PER_CONNECTOR = max(1, MAX_ACTIVE_PROMPTS // 3)` — 2 at the default pool size, 1 at the minimum |
| Request text frame | 256 KiB |
| Outgoing text chunk | 8192 characters (up to 32 KiB UTF-8 before JSON encoding) |
| Prompt text | 128 KiB |
| Output retained per prompt | 8 MiB |
| Created/loaded sessions per connection | 100 |
| Replay history | 2000 stored messages and 8 MiB of stored text; larger history fails explicitly |
| Incoming rate | burst 60, refill 10 messages/second |
| WebSocket send timeout | 15 seconds |
| Idle connection timeout | 600 seconds with no pending request |
| Active prompt timeout | 1800 seconds |

The two admission rows are a slice of the shared SQLAlchemy pool (`DB_POOL_SIZE`, see Configuration below), not free-floating constants: each admitted prompt or replay pins one physical pooled connection for up to the active prompt timeout (`session_lease` → `leader_session`), so the budget must stay meaningfully below `DB_POOL_SIZE` to leave headroom for ordinary REST traffic, Socket.IO and the schedulers. Raising ACP concurrency means raising `DB_POOL_SIZE`, not these constants directly; a `model_validator` on `Settings` rejects `DB_POOL_SIZE < 8`. The per-connector row is an independent budget from `connector.max_connections` in the row above — the former bounds concurrent long-lived DB checkouts admitted process-wide per connector, the latter bounds concurrent WebSocket connections to that connector (default 10). At the pool floor all ten of a connector's connections can be open at once, but only one prompt across the whole process (and at most one from that connector) is admitted at a time; the rest receive `-32004` (`"Server prompt capacity reached"` once the global cap is hit, `"Connector prompt capacity reached"` once only that connector's sub-quota is hit).

Pending requests are exempt from the idle connection timeout; active prompts use their separate execution limit. Duplicate in-flight request IDs, binary frames, oversized frames, or rate-limit violations terminate the peer; JSON parse/shape errors return JSON-RPC errors. Limits are intentionally finite; this profile does not provide resumable event delivery.

## Frontend components

- `frontend/src/components/Agents/Acp/AcpConnectorsCard.tsx`: generated-client query/mutations, five-row PreviewList, shared full-list Sheet, owner-controlled dialogs and connector deletion.
- `frontend/src/components/Agents/Acp/AcpConnectorRow.tsx`: connector state/mode, copy endpoint and overflow actions.
- `frontend/src/components/Agents/Acp/AcpConnectorForm.tsx`: create/edit name, mode and maximum connections; updates send only dirty fields to preserve concurrent edits to other settings.
- `frontend/src/components/Agents/Acp/AcpConnectionDetails.tsx`: endpoint, remote cwd, authentication and hosted-profile details.

`frontend/src/components/Agents/Acp/AcpTokensDialog.tsx` provides token management as a controlled dialog with list, issuance, and one-time-secret states. It does not persist secrets in browser storage or React Query mutation result caches. Generated client methods supply all management contracts; query invalidation refreshes affected lists.

Run `npm run test:acp:ui` from `frontend` to exercise the real components with mocked API responses: concurrent edits, token erasure, revocation confirmation, bounded previews, and failure/retry states. Backend protocol tests use the real mounted WebSocket and official SDK client with only the external agent environment replaced by a test double. Migration tests use their own scratch database.

## Configuration and client entry points

- `ACP_SERVER_BASE_URL` in `backend/app/core/config.py` is the public base including `/acp`, for example `wss://api.example.com/acp`. Connector projection appends its UUID; http/https bases are converted to ws/wss. An unset value produces a null public endpoint rather than trusting an incoming Host header.
- `.env.example` and `docker-compose.yml` expose this setting. The server route exists independently of URL projection; deployment must route `/acp/` WebSocket upgrades to the backend.
- The shared SQLAlchemy engine (`backend/app/core/db.py`) is sized from settings rather than SQLAlchemy's 5+10 default, because it now also serves ACP's long-lived prompt checkouts alongside ordinary request traffic: `DB_POOL_SIZE` (20), `DB_MAX_OVERFLOW` (10), `DB_POOL_TIMEOUT` (30, seconds a caller waits for a free connection), `DB_POOL_RECYCLE` (1800, seconds before a pooled connection is retired), plus `pool_pre_ping=True` (probes a connection before handing it out, at the cost of one extra round trip per checkout, so a connection the server already closed is replaced instead of surfacing as an `OperationalError` mid-request). `DB_POOL_SIZE + DB_MAX_OVERFLOW` is the per-worker connection ceiling; keep `workers × ceiling` comfortably under Postgres `max_connections` (100 by default). `DB_POOL_SIZE` also drives the ACP prompt-admission budget above, so `Settings` rejects a value below 8.
- Existing Docker Compose runs one backend worker for process-local protocol state. Connection counts and SDK objects are per worker; a multiworker deployment does not enforce a global connector connection limit. Shared PostgreSQL session leases protect concurrent ACP session operations, not an HA transport registry.
- Use WSS for remote deployment and preserve WebSocket upgrade headers at the proxy. ACP HTTP/SSE is not mounted and does not require switching Uvicorn to HTTP/2.
- Apply the migration before exposing management or ACP routes. Downgrade drops the two ACP tables and preserves ordinary session history.

`backend/clients/acp/client.py` demonstrates the pinned official Python SDK: set `CINNA_ACP_URL` and `CINNA_ACP_TOKEN` in the process environment and run `uv run backend/clients/acp/client.py`. It initializes, creates a hosted session, sends a text prompt, prints streamed agent text, and closes cleanly.

For a stdio ACP client, configure its agent command as `uv run` with the absolute path to `backend/clients/acp/stdio_bridge.py`, supplying the same two environment variables. The bridge emits only JSON-RPC on stdout, reports generic failures on stderr, and rewrites new/load cwd to the remote workspace. It leaves nonempty client MCP lists intact for the server to reject. The POSIX bridge supports macOS/Linux pipes with asynchronous stdout backpressure and a 15-second drain timeout. The backend image includes the client scripts and the development override mounts them for tests. Bridge/client URL validation requires WSS except for loopback WS and rejects embedded credentials, queries, and fragments.

## Issued Credentials listing

`ACPConnectorService.list_issued_by` returns tokens under connectors the caller owns (on agents the caller owns) as `IssuedCredentialPublic` rows (kind `acp_token`, `details`: `connector_id`, `connector_name`, `mode`) for `GET /credentials/issued`. Status: revoked > expired > paused > active; paused when the connector, agent or owner is inactive or `_mode_allowed` fails, the same check `authenticate` applies. See [Credential Sharing (tech)](../../agents/agent_credentials/credential_sharing_tech.md).

## Changelog

- 2026-09-12: Sized the shared DB pool explicitly (`DB_POOL_SIZE`/`DB_MAX_OVERFLOW`/`DB_POOL_TIMEOUT`/`DB_POOL_RECYCLE`, `pool_pre_ping=True`) and derived the prompt-admission budget from it instead of a fixed constant; added a per-connector sub-quota beneath the global cap; memoized per-chunk streaming authorization for 1s behind the unchanged 5s watchdog guarantee; let the watchdog tolerate up to 2 consecutive unexpected (non-authorization) failures before stopping a turn; clarified the revocation copy on token revoke/delete.
- 2026-11-17: Added `list_issued_by` and the shared `_mode_allowed` check.

See the [official ACP transport specification](https://agentclientprotocol.com/protocol/v1/transports) and [Python SDK remote transport documentation](https://agentclientprotocol.github.io/python-sdk/web-transport/) for upstream transport status. Cinna's text-only hosted profile deliberately rejects local MCP-server injection and does not claim unrestricted ACP filesystem/terminal conformance.
