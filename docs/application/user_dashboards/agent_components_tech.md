# Agent Components — Technical Reference

Business logic: [Agent Components](agent_components.md). Parent feature: [User Dashboards tech](user_dashboards_tech.md).

## Database

Migration `backend/app/alembic/versions/056d6b9d9f87_add_dashboard_components.py`. Models in `backend/app/models/dashboards/dashboard_component.py`.

| Table / column | Purpose |
|----------------|---------|
| `user_dashboard_block_result` (`UserDashboardBlockResult`) | One row per block (`block_id` PK, FK cascade): `result` (JSON text, capped at 5 MiB), `fetched_at`, `params_hash`, `mark` (<= 500 chars, one-sentence failure mark) and `mark_at`, `consumed_schedule_log_id` |
| `dashboard_tile_runtime` (`DashboardTileRuntime`) | Budget and page state per target: `target_key` PK VARCHAR(400), `run_times` (JSON list of epoch seconds), `page_doc`, `highlight_ready`, `page_seq` (BIGINT, monotonic), `page_state_at`, `updated_at` |
| `session.dashboard_id` | UUID, FK `session_dashboard_id_fkey` ON DELETE SET NULL, indexed; backfilled from `dashboard_block_id`, joined through `user_dashboard` so only the owner's dashboards are linked |

`user_dashboard_block.view_type` gains `agent_component` (`DashboardBlockViewType`, `backend/app/models/users/user_dashboard.py`). Its `config` is `AgentComponentBlockConfig`: `component`, optional `skill`, `params`, and `refresh` (`AgentComponentRefreshOverrides`: `max_age`, `while_open_every` as a duration of at least 1m or `"manual"`, `after_turn`; `on_app_start` is never stored). Dashboard chat shapes live in `backend/app/models/dashboards/dashboard_chat.py`; `MessageCreate.dashboard_context` and `SessionPublic.dashboard_id` in `backend/app/models/sessions/session.py`. Events `dashboard_tile_updated` and `dashboard_tile_highlight` in `backend/app/models/events/event.py`.

Budget target keys: `b:<block_id>` for a block; `p:<agent_id>:<skill|->:<component>[:<tile_key>]` for the owner preview.

## Routes (backend, `/api/v1`)

Auth is `CurrentUser` unless noted. Errors share one body, `{"error": {"code", "message", "retry_after_s?", "field?"}}`, built in `backend/app/api/routes/_dashboard_component_errors.py`: `invalid_input` 400, `not_found` 404, `needs_credentials` / `adapter_unsupported` 409, `rate_limited` 429 (+`Retry-After`), `call_failed` 502, `env_unavailable` 503.

| Method | Path | File | Purpose |
|--------|------|------|---------|
| GET | `/agents/{agent_id}/dashboards/` | `backend/app/api/routes/agent_dashboards.py` | List the agent's components with findings and placements |
| GET | `/agents/{agent_id}/dashboards/{name}` | same | One component incl. `component_json` |
| POST | `/agents/{agent_id}/dashboards/{name}/run` | same | Owner preview run of an endpoint or source (`skill`, `endpoint`, `params`, `tile_key`) |
| POST | `/agents/{agent_id}/dashboards/{name}/frame` | same | Mint a preview frame cap and URL |
| GET | `/dashboards/{dashboard_id}/blocks/{block_id}/component/data` | `backend/app/api/routes/dashboard_tiles.py` | Cached data plus `stale`, `mark`, `notice`, `params_resolved`, `missing_credentials`, `skipped` |
| POST | `.../component/refresh` | same | `{reason}`: `open`, `reshow`, `manual`, `interval`, `after_turn`, `params` |
| PUT | `.../component/params` | same | Replace the whole param set |
| POST | `.../component/call` | same | Page call `{endpoint, params}` (budgeted) |
| POST | `.../component/frame` | same | Mint a block frame cap and URL |
| PUT | `.../component/page-state` | same | `{page_doc, highlight_ready, seq?}`, 204; a write with a lower `seq` than stored is ignored |
| GET | `/dashboards/capabilities` | `backend/app/api/routes/user_dashboards.py` | Limits and feature flags for clients (see below); `CurrentUser`, never touches an environment |
| GET | `/dashboards/{dashboard_id}/chat?agent_id=` | `backend/app/api/routes/dashboard_chat.py` | `{session \| null}` |
| POST | `/dashboards/{dashboard_id}/chat` | same | `{agent_id, new?}` get-or-create |
| POST | `/dashboards/component-refs/resolve` | same | Up to 20 refs, optional `session_id` |
| POST | `/agent/dashboards/tiles` | `backend/app/api/routes/agent_dashboard_tools.py` | Agent-env token; body `{session_id}`; always 200 with result or `{error}` |
| POST | `/agent/dashboards/update-tile` | same | Agent-env token; `{session_id, tile_id, params?, highlight?}` |

`GET /dashboards/capabilities` (`backend/app/services/dashboards/capabilities.py`; models `DashboardsCapabilities` / `DashboardsLimits` in `backend/app/models/dashboards/dashboard_capabilities.py`) returns `{version: 1, contract_version, limits: {runs_per_minute, blocks_per_dashboard, dashboards_per_user, context_chars, summary_chars, stdout_bytes, timeout_s_default, timeout_s_max}, chat: true, component_refs: true}`. Values come from `run_budget.MAX_RUNS_PER_WINDOW`, `user_dashboard_service.MAX_BLOCKS_PER_DASHBOARD` / `MAX_DASHBOARDS_PER_USER` (the web/cloud cap; desktop-origin boards use `MAX_DESKTOP_DASHBOARDS_PER_USER` = 50), `dashboard_context.MAX_CHARS` / `SUMMARY_MAX_CHARS`, and `ENDPOINT_*` constants that mirror env-core `endpoint_runner.py` (pinned equal by `backend/tests/unit/test_dashboards_capabilities_parity.py`). `contract_version` is `LocalAgentKitService.get_contract_version()`, null when the kit cannot be served. The route is declared before the `/{dashboard_id}` routes, and is reachable through the account api-proxy.

**Error body difference.** `POST /dashboards/{dashboard_id}/chat` (`dashboard_chat.py`) still refuses with `HTTPException` and a `{detail}` body, not the `{error: {code, message}}` shape above, because Cinna Desktop's `coreDashboardsClient.ts` reads `detail` from it. This is deliberate. The platform-knowledge `api_reference/DashboardChat.md` is generated from OpenAPI and cannot carry this note; it lives here.

**CLI workspaces.** The account api-proxy denies the two `.../frame` mints and `PUT .../component/page-state` (see [Account CLI Workspace tech](../cinna_cli_integration/account_cli_workspace_tech.md)); `run`, `data`, `params`, `call`, `refresh` and GETs stay allowed. The CLI building context (`GET /cli/agents/{agent_id}/building-context`, see [CLI Integration tech](../cinna_cli_integration/cinna_cli_integration_tech.md)) passes the agent's `bundle_id` to env-core so the Dashboards section renders `cinna://component/bundle:<bundle_id>/<name>`, and lists `platform_skills: [{name, kit_path}]` (`skills/<name>` in the local kit) so the CLI can point at the context package's `context/local-kit/<kit_path>/`.

`dashboard_chat` is registered before `user_dashboards` in `backend/app/api/main.py` so `/dashboards/component-refs/resolve` is not read as a `{dashboard_id}`. Block add/update in `backend/app/api/routes/user_dashboards.py` are async and call `add_block_checked` / `update_block_checked` (`backend/app/services/users/user_dashboard_service.py`), which validate component, params and credentials, and drop the result row when the component or skill changes.

The messages route (`backend/app/api/routes/messages.py`) sanitises `dashboard_context` (400 on bad JSON or more than 12,000 chars) and silently drops it when the session has no `dashboard_id`.

Over A2A (both `/a2a/{agent_id}/` and `/external/a2a/agent/{agent_id}/`; Cinna Desktop's dashboard panel uses the latter with the user's JWT and the chat session id as the task id), the same context rides in `message.metadata.cinna_dashboard_context` (a JSON object, or a JSON string). `A2ARequestHandler._check_message_session` (`backend/app/services/a2a/a2a_request_handler.py`) applies the same rules through `dashboard_context.from_a2a_metadata` and `sanitize`: dropped unless the resumed session has a `dashboard_id` (a new session never does), a bad one is JSON-RPC `-32602` on stream and send, and the text is passed to `send_session_message(dashboard_context=)` (through `ChannelIngestionService.ingest_inbound_message` for `message/send`), so storage and the sha256 dedupe are the messages route's. The same check refuses a task id that names another agent's session (`-32001`).

Placements (`ComponentService.placements`, component list and get, and component-ref `placements`) carry the dashboard's `origin`, so a desktop dashboard (`origin: "desktop"`, see [User Dashboards tech](user_dashboards_tech.md)) can be named as Cinna Desktop; `ComponentReferenceCard` shows those once, unlinked.

## Services (`backend/app/services/dashboards/`)

| Module | Role |
|--------|------|
| `component_service.py` | `ComponentService`: env wake via `WebappService`, per-worker component list cache (`LIST_CACHE_TTL_S = 10`; `list_components(..., fresh=True)` bypasses it, used by tile tools, ref resolve, manual / `after_turn` / `params` refresh, `call` and `set_params`), `require_endpoint`, run / source / `refresh.command` (120 s), credential check through `InstallReadinessGate`'s `service_credential_readiness`, `touch_activity`; `DashboardComponentError` |
| `component_params.py` | Param resolution (`current`/`today` in `user.timezone`, else UTC), validation sentences, `params_hash` |
| `run_budget.py` | `check_and_record` (30 per 60 s, row locked with `INSERT ... ON CONFLICT DO UPDATE` then read), idle-row prune, `set_page_state` / `get_page_state`; readiness counts as false when `page_state_at` is older than `PAGE_STATE_TTL` (15 min) |
| `tile_service.py` | `TileService`: `get_data`, `refresh`, `set_params`, `call`, `frame_entry`, `set_page_state`; deduplicated recompute; an `interval` tick returns the cache when `fetched_at` is under 60 s old; `open` / `reshow` wake the environment before the staleness check; lazy schedule check (`_unconsumed_schedule_log`: a log counts once settled, session-triggered logs once their session is not `running`/`pending_stream`) |
| `agent_component_service.py` | Backs the agent-scoped owner routes |
| `frame_cap.py` | `mint`, `verify`, `parse_target`, `frame_url` |
| `frame_content.py` | `/d/` serving, CSP and bridge injection |
| `dashboard_chat_service.py` | `get`, `get_or_create`; marker `CHAT_MARKER_KEY = "dashboard_chat"`, latest chosen by `created_at` |
| `dashboard_context.py` | `sanitize`, `digest`, `block`, `last_sent(before_sequence=)`, `focused` |
| `tile_tools_service.py` | `tiles`, `update_tile`, highlight emit, background `_recompute_and_notify` |
| `component_refs_service.py` | `parse`, `resolve`; manifest read from the host workspace copy of `cinna-agent.json` |

Adapter methods (4 abstract in `backend/app/services/environments/adapters/base.py`, implemented in `docker_adapter.py`) call the env-core routes below. A bare 404 from the environment is `EndpointUnsupportedError`, surfaced as `adapter_unsupported`.

### Message assembly

`backend/app/services/sessions/message_service.py`: `_build_session_context` adds `dashboard_id` to the signed session context; `collect_pending_messages` and `_build_llm_batch_content` append the `<dashboard_context>` block, full or omitted by sha256 of the last block sent before the batch (`_dashboard_context_digest_ref`). The four tile tool names are in `PRE_ALLOWED_TOOLS`. `SessionService.create_session(dashboard_id=)` stores the link; a session started from one of the user's own blocks gets that block's dashboard.

## Frame content host `/d/v/<cap>/<path>`

- Route: `backend/app/api/routes/dashboard_frame_content.py`, GET and HEAD; other methods get 405 with `Allow: GET, HEAD`; excluded from OpenAPI.
- Origin: `settings.artifacts_content_origin`. `ArtifactContentMiddleware` (`backend/app/core/artifact_content_middleware.py`, `CONTENT_PREFIXES = ("/a/", "/d/")`) answers preflight and gives unmatched `/d/` paths a 404 with the frame sandbox headers.
- Cap: HS256 JWT signed with `SECRET_KEY`, `aud=dashboard_frame`, `sub=user_id`, `tgt` as above; TTL `DASHBOARD_FRAME_CAP_TTL_SECONDS` (default 43200, `backend/app/core/config.py`). Cookies are never used; opaque-origin frames authenticate by the cap alone and ownership is re-checked per request.
- Serve order: verify cap, re-check ownership (block: dashboard owner and `agent_component` view; preview: agent owner), path check (404 for `api` first segment, dot or empty segments, leading `/`, backslash, NUL), running environment with `activate=False` (503 if not running), component lookup and entry page (404 if missing or native; compared after `posixpath.normpath`), then `read_dashboard_file` (404 stays 404, other failures 502). The file read is by path alone through env-core `/dashboards/file`.
- Bridge: the exact bytes of `protocols/dashboards-bridge.js` from the kit snapshot are injected into the entry page only, before the first `<script` and after `<head>` (comments blanked while searching; falls back to after `<html>`, the doctype, a UTF-8 BOM, or offset 0). `touch_activity` runs on GET only.
- Headers: CSP `sandbox allow-scripts allow-forms allow-modals; default-src 'none'; connect-src/script-src/style-src/img-src/font-src/media-src` scoped to `<origin>/d/v/<cap>/` (never `'self'`), `form-action 'none'; base-uri 'none'; frame-src 'none'; worker-src 'none'; frame-ancestors <SPA origin>`; plus `Access-Control-Allow-Origin: *`, `nosniff`, `no-referrer`, `no-store`. Error responses use the same sandbox with `default-src 'none'`.

### Log and telemetry suppression

The path carries the cap and may carry page data, so `/d/` is never logged:

- nginx: `location /d/` with `access_log off; log_not_found off; error_log /dev/null crit;` ([Nginx Setup](../../infrastructure/nginx_setup.md)).
- `backend/app/core/access_log_filter.py`: `DropUnloggedPathsFilter` on `uvicorn.access` drops `/d/` records (installed at lifespan start in `backend/app/main.py`).
- Sentry: `sentry_before_send` and `sentry_before_send_transaction` drop `/a/` and `/d/` transactions, reduce request URLs to `<origin>/d/[Filtered]`, remove query strings, scrub breadcrumb and message URLs, and drop frame `vars` for those events.
- Handler errors are logged by tile id only.
- Environment side: all dashboards routes take POST bodies, so env-core's own access log sees only fixed paths; `core/main.py` also filters `/dashboards/` from `uvicorn.access`.

## Env-core (inside the environment)

`backend/app/env-templates/app_core_base/core/server/`:

- `endpoint_runner.py`: `run_script(script, params, timeout_s, cwd, use_uv)` returning `RunResult{ok, value_bytes, exit_code, stderr_tail, reason}` (`ok|timeout|too_large|failed|bad_json`). Params go on stdin (EPIPE tolerated), stdout capped at `MAX_STDOUT_BYTES` (5 MiB), stderr tail 2000 chars, the whole process group is killed on timeout, runs use a dedicated 8-thread pool with a semaphore and a backstop of timeout + 10 s. Timeout clamps to 1..300 s. `.py` runs `python`, or `uv run --quiet python` when `use_uv` and a `pyproject.toml` exists in the agent root; `.js` runs `node`; `.ts` and others raise `UnsupportedScriptError`.
- `dashboards_routes.py`, included from `routes.py` behind `verify_auth_token`; all POST, JSON body, `Cache-Control: no-store`: `/dashboards/components`, `/dashboards/file`, `/dashboards/run`, `/dashboards/source-file`. `/file` refuses `api` first segment, dot or empty segments, leading `/`, backslash and any realpath escape (400 `invalid_input`; missing is 404 `not_found`); `/run` always answers 200 `{ok, value}` or `{ok:false, code, message, stderr_tail?}`; `/source-file` reads `source.file`, which must be under `app-data/` or `files/` of the workspace. Symlinked `dashboards/` or root folders are refused. Skills resolve through `AgentEnvService.build_skills_index()`; an agent's own copy wins on a shadowed name.
- `routes.py` `webapp_data_api` uses the same runner with `use_uv=False` (see [Agent Webapp tech](../../agents/agent_webapp/agent_webapp_tech.md)).
- Tools: `tools/cinna_dashboards_protocol.py` loads specs from `protocols/dashboards-tools.schema.json`, posts `{session_id, ...}` to `/api/v1/agent/dashboards/<tool>` and maps `{error}` to an error result; on a non-200 it logs only the status and `error.code`. `verified_session_context` / `verified_dashboard_id` require the HMAC signature; without it Claude gets no dashboard tools. Claude binding: toolset `dashboards` in `tools/cinna_tasks_claude.py`; OpenCode binding: `tools/mcp_bridge/cinna_server.py` (reads a per-session `session_context/<id>.json`, falling back to the shared file when the plugin is absent), with `tools/mcp_bridge/cinna_session_plugin.js` stamping the OpenCode session id on `cinna_dashboards_*` calls (registered in the runtime `opencode.json` `plugin` list), listing the tools in conversation mode (`CINNA_OPENCODE_MODE`) when the schema file exists. Names (`mcp__cinna__dashboards_tiles`, `mcp__cinna__dashboards_update_tile`, `cinna_dashboards_*`) are in `adapters/tool_name_registry.py`. `prompt_generator.py` adds a Dashboards section when `/app/core/skills/cinna-dashboards/SKILL.md` exists.

## Frontend

Generated client: `AgentDashboardsService`, `DashboardTilesService`, `DashboardChatService` in `frontend/src/client/sdk.gen.ts`. `frontend/vite.config.ts` proxies `^/d/` to the backend.

`frontend/src/components/Dashboard/AgentComponents/`:

| File | Role |
|------|------|
| `AgentComponentView.tsx` | Shared view, `mode: "tile" \| "preview"`; wired for tiles by `Dashboard/UserDashboards/views/AgentComponentView.tsx` |
| `useAgentComponentTile.ts` | Tile data query (re-read every 30 s while visible), refresh and params mutations (only the latest params PUT is applied; a fresher `fetched_at` wins), socket handlers |
| `useTileRefreshDriver.ts` | `open`, `reshow` and `interval` driver; `IntersectionObserver` plus `visibilitychange` |
| `useAgentComponent.ts`, `componentSpec.ts`, `tileConfig.ts`, `tileController.ts` | Component read, spec helpers, block config read/write, controller used by both tile and preview |
| `bridgeHost.ts` | The page-side protocol host (nonce per `ui/initialize`, `event.source` identity, `tools/call`, `ui/message`, `ui/open-link`, `collectTileContext(blockId, timeoutMs = 500)`) |
| `HtmlFrameView.tsx` | Frame mint (`/frame`) and `FrameBridge` attach; sends current data right after attach. Page-state PUTs carry a `seq` (microseconds since the epoch, always increasing across reloads) and are queued per tile; re-sent every 10 min while attached and on bfcache return; on detach and `pagehide` a `fetch` with `keepalive` and the Bearer header clears readiness |
| `highlightStore.ts` | In-memory per-block highlight: `setHighlight`, `clearHighlight`, `stepHighlight`, `subscribeHighlight`, `useTileHighlight` |
| `dashboardFocus.ts` | `useDashboardFocus(id)` -> `{focusedBlockId, focusSeq, focusTile}`, `useFocusedTileContext`, `useTileContexts` (sorted by block id), `registerDashboardChatPanel(id, {open, seedComposer})`, `useHasDashboardChat` |
| `NativeListView`, `NativeTableView`, `NativeMetricsView`, `nativeRows.ts` | Native views; the table is a `<table role="grid">` |
| `TileFooter`, `ParamsRow`, `ComponentPicker`, `RefreshOverridesSection`, `OpenLinkConfirm` | Tile and dialog pieces |

`frontend/src/components/Dashboard/DashboardChat/`: `DashboardChatPanel` (layout, context collection), `DashboardAgentChat` (get-or-create session), `DashboardChatConversation`, `ContextChip`, `PromptStarters`, `DashboardChatToggle`, `useDashboardChatLayout`, `dashboardContext.ts` (payload build and size trimming), `ComponentPreviewDialog` with `usePreviewController.ts`, `AddComponentToDashboardDialog`. Route: `frontend/src/routes/_layout/dashboards/$dashboardId.tsx`. `useSessionStreaming.sendMessage` takes `dashboardContext` as a sixth argument.

`useTileRefreshDriver` floors "while open" at 60 s. On `stream_completed` for a panel session, `DashboardChatPanel` invalidates `["agentDashboardComponent", agentId]`, the component list and the session's `componentRef` cards.

Chat side: `frontend/src/components/Chat/ComponentReferenceCard.tsx`, `componentRefBatcher.ts`, `componentRefContext.ts`, `frontend/src/utils/componentRefs.ts`, and `MarkdownRenderer.tsx`.

Query keys: `tileDataQueryKey(dashboardId, blockId)`, `["userDashboard", dashboardId]`, `["userDashboards"]`, `componentsQueryKey(agentId)`, `agentComponentQueryKey(agentId, name, skill)`, `["agentComponentPreview", agentId, name, skill, paramsJson]`, `dashboardChatQueryKey(dashboardId, agentId)` = `["dashboardChat", dashboardId, agentId]`, `componentRefQueryKey(ref, sessionId)` = `["componentRef", ref, sessionId]`, plus `["session", id]` and `["messages", id]` for the conversation.

Socket handlers (`useEventSubscription`), matching on `event.meta.block_id`: `dashboard_tile_updated` invalidates the tile data and the dashboard (the agent may have changed the params); `dashboard_tile_highlight` writes `event.meta.ids` to the highlight store.

## Tests

`backend/tests/api/dashboards/` (agent component, frame, chat, tile tool, refs, desktop dashboard scenarios), `backend/tests/api/external/external_a2a_dashboard_chat_test.py` and `backend/tests/api/a2a_integration/test_a2a_dashboard_context.py` (dashboard context over A2A); unit tests `backend/tests/unit/test_dashboard_frame_content.py`, `test_env_endpoint_runner.py`, `test_env_dashboards_routes.py`, `test_cinna_dashboards_tools.py`, `test_local_kit_dashboards.py`.
