# Server Channels — Technical Reference

## File Locations

### Backend — models
- `backend/app/models/server_channels/server_channel.py` — `ServerChannel` (table), `ServerChannelBase`, `ServerChannelCreate`, `ServerChannelUpdate`, `ServerChannelPublic`, `ChannelTypePublic`, `ChannelTestOutboundRequest`, `ChannelTestOutboundResult`, `ChannelSetupInstructions`
- `backend/app/models/server_channels/server_auto_install_bundle.py` — `ServerAutoInstallBundle` (table), `AutoInstallBundleAdd`, `AutoInstallBundlePublic`
- `backend/app/models/server_channels/channel_thread_binding.py` — `ChannelThreadBinding` (table), status constants `CHANNEL_BINDING_PENDING_INSTALL` / `CHANNEL_BINDING_ACTIVE` / `CHANNEL_BINDING_FAILED`
- Re-exported from `backend/app/models/__init__.py`
- `backend/app/models/sessions/session_sender.py` — `SessionSender.from_channel()`, `channel_caller` added to `SessionSenderKind`, `ChannelAccessPolicy` (pre-existing dataclass, reused)

### Backend — services
- `backend/app/services/server_channels/server_channel_service.py` — `ServerChannelService`: admin CRUD, secrets, webhook token, auto-install list
- `backend/app/services/server_channels/channel_inbound_service.py` — `ChannelInboundService`: the webhook → binding → session pipeline; it *composes* the routing decision rather than making it
- `backend/app/services/server_channels/channel_routing_service.py` — `ChannelRoutingService`: both routing passes and the `decide()` entry point they sit behind. Split out of `ChannelInboundService` by [Auto Routing Tuning](../routing_tuning/routing_tuning_tech.md)'s Phase 3 so that `POST /admin/routing/simulate` can route without being able to bind, install or reply — see the section below
- `backend/app/services/routing/channel_candidate_provider.py` — `ChannelCandidateProvider`: Pass 1's candidate set, built from `select(Agent).where(Agent.owner_id == user_id)`. Lives under `app/services/routing/` (not `server_channels/`) because it is the seam the [channel routing scope split](../../plans/channel_routing_scope_split_plan.md) introduced to keep candidate construction out of both `channel_routing_service.py` and the App MCP / Identity providers
- `backend/app/services/server_channels/channel_outbound_service.py` — `ChannelOutboundService`: `STREAM_COMPLETED`/`STREAM_ERROR` subscribers, `notify_progress`
- `backend/app/services/server_channels/channel_pending_scheduler.py` — APScheduler job, `TESTING`-gated, calls `ChannelInboundService.flush_pending_bindings`
- `backend/app/services/server_channels/adapters/base.py` — `ChannelAdapter` ABC, `ChannelInboundMessage`, `ChannelCapabilities`, error types
- `backend/app/services/server_channels/adapters/registry.py` — `CHANNEL_ADAPTERS`, `get_adapter()`, `available_channel_types()`
- `backend/app/services/server_channels/adapters/google_chat.py` — `GoogleChatAdapter`
- `backend/app/services/sessions/channel_ingestion_service.py` — pre-existing `ChannelIngestionService`; extended with `channel_caller` handling in `assert_access`, `_select_session_owner_id`, `_verify_resume_sender`
- `backend/app/services/common/email_patterns.py` — new shared module, `match_email_pattern()` (extracted from the email integration's `routing_service.py`)
- `backend/app/services/users/user_service.py` — `UserService.create_external_user()` (new shared helper, migrated the email integration's `create_email_user` onto it)
- `backend/app/core/security.py` — `verify_google_signed_jwt()` (new, generalized), `_get_google_certs()` (generalized JWKS fetch/cache), `GoogleCertsUnavailable` (new exception), `verify_google_token()` (existing OAuth path, now built on the shared verifier)

### Backend — routes
- `backend/app/api/routes/server_channels.py` — public webhook + superuser admin CRUD, registered in `backend/app/api/main.py` (`tags=["server-channels"]`)

### Backend — other
- `backend/app/models/events/security_event.py` — `SERVER_CHANNEL_CREATED`, `SERVER_CHANNEL_UPDATED`, `SERVER_CHANNEL_DELETED`, `SERVER_CHANNEL_TOKEN_REGENERATED`, `SERVER_CHANNEL_VERIFICATION_FAILED`, `SERVER_CHANNEL_SENDER_DENIED`, `SERVER_CHANNEL_USER_AUTO_REGISTERED`, `SERVER_CHANNEL_AUTO_INSTALL`
- `backend/app/services/common/egress_guard.py` — pre-existing `assert_url_allowed()`, reused by the Google Chat adapter's outbound calls (Chat API + token endpoint)
- `backend/app/core/config.py` — `SERVER_CHANNEL_WEBHOOK_MAX_BODY_BYTES`, `SERVER_CHANNEL_WEBHOOK_RATE_LIMIT_PER_MIN`
- `backend/app/services/common/rate_limiter.py` — pre-existing `RateLimiter`, one process-local instance keyed by webhook token

### Frontend
- `frontend/src/components/Admin/ServerChannels/ServerChannelsCard.tsx` — channel list: enable/disable switch, edit/delete, empty state, per-row amber badges
- `frontend/src/components/Admin/ServerChannels/ServerChannelDialog.tsx` — two-step add/edit dialog: type picker, then that type's form (edit skips step 1 — the type is immutable)
- `frontend/src/components/Admin/ServerChannels/ChannelTypePicker.tsx` — step 1: one card per registered adapter (icon, display name, tagline)
- `frontend/src/components/Admin/ServerChannels/ServerChannelForm.tsx` — step 2: react-hook-form + zod, config fields and schema built from the type's registry entry
- `frontend/src/components/Admin/ServerChannels/channelTypes.ts` — per-`channel_type` presentation (icon, tagline) + config-field descriptors; unknown types fall back to a raw JSON config editor
- `frontend/src/components/Admin/ServerChannels/ChannelSetupInstructionsPanel.tsx` — webhook URL (copy button), adapter-provided setup steps, test-outbound control, token regeneration (confirm dialog)
- `frontend/src/components/Admin/ServerChannels/AutoInstallAgentsCard.tsx` — auto-install list: catalog-backed add-picker, visibility/no-trigger-prompt amber badges, remove
- `frontend/src/components/Admin/ServerChannels/channelCopy.ts` — shared whitelist/help copy + `parseWhitelist()` (tokenizing whitelist parser mirroring the backend matcher, used to render the "blanket allow" warning correctly for every spelling of `*`)
- `frontend/src/routes/_layout/admin/server-configuration.tsx` — adds the `channels` `HashTabs` entry rendering `ServerChannelsCard` + `AutoInstallAgentsCard`

### Migration
- `backend/app/alembic/versions/1a17557c0311_add_server_channels_tables.py` — creates `server_channel`, `server_auto_install_bundle`, `channel_thread_binding`. Hand-corrected after autogenerate: unrelated pre-existing local-DB drift (dropping `session.channel_*`, `app_agent_route.channels`, etc.) was deliberately excluded.

### Tests
- `backend/tests/api/server_channels/` — `conftest.py`, `server_channels_security_invariants_test.py`, `server_channels_admin_test.py`, `server_channels_webhook_test.py`, `server_channels_routing_test.py`, `server_channels_pending_outbound_test.py`, `README.md` (read first — explains the fixtures and domain-specific test patterns)
- `tests/unit/test_google_chat_adapter_chunk.py` — pure-logic unit test for `GoogleChatAdapter._chunk`
- `tests/utils/server_channel.py` — `GoogleChatJWTSigner` (real RS256 signer + JWKS for tests, only the fetch is patched), `flush_pending_bindings` test helper

## Database Schema

### `server_channel`
One row per configured channel instance. Key fields: `channel_type` (indexed, must resolve in `CHANNEL_ADAPTERS`), `name` (unique), `enabled` (disabled ⇒ webhook 404s, same as unknown), `config` (JSON, adapter-specific non-secret config — Google Chat: `{"project_number": "..."}`), `encrypted_secrets` (Fernet-encrypted, nullable, write-only), `email_whitelist` (Text, nullable — null/empty is fail-closed deny-all), `webhook_token` (unique, 64 chars, `secrets.token_urlsafe(32)`), `created_by` (FK `user.id`, `ON DELETE SET NULL`). Unique constraints on `name` and `webhook_token`.

### `server_auto_install_bundle`
Server-wide (not per-channel) auto-install candidate list. `bundle_uuid` (FK `agent_bundle.id`, `ON DELETE CASCADE`, unique — one row per bundle), `added_by` (FK `user.id`, `ON DELETE SET NULL`).

### `channel_thread_binding`
The feature's conversation state. `server_channel_id` (FK `server_channel.id`, `ON DELETE CASCADE`), `thread_key` (channel-native thread id, e.g. `spaces/AAA/threads/BBB`; unique together with `server_channel_id` — this is the race guard for two first-messages in one new thread), `user_id` (FK `user.id`, `ON DELETE CASCADE`, indexed), `agent_id` (FK `agent.id`, `ON DELETE CASCADE` — uninstalling the agent cascades the binding away), `session_id` (FK `session.id`, `ON DELETE SET NULL`, indexed, nullable — null before first activation and after the session is deleted), `status` (`pending_install` / `active` / `failed`), `pending_messages` (JSON list of `{text, external_message_id, external_user_id, received_at}`, capped at 20 entries — mutate via reassignment + `flag_modified`, never in-place `.append()`), `last_external_message_id` (webhook redelivery dedup), `last_error` (Text, nullable).

No changes to any pre-existing table.

## API Endpoints

All routes in `backend/app/api/routes/server_channels.py`, `tags=["server-channels"]`.

### Public (no auth dependency)
- `POST /api/v1/channels/{webhook_token}/inbound` — the one unauthenticated ingress on the platform. Rate-limited (`SERVER_CHANNEL_WEBHOOK_RATE_LIMIT_PER_MIN` per token) and body-size-capped (`SERVER_CHANNEL_WEBHOOK_MAX_BODY_BYTES`) before the token is even resolved. Returns the adapter's sync-response body (`{}` for most outcomes; Google Chat renders `{"text": ...}` in-thread). 404 for an unknown *or* disabled token / an adapter no longer in the registry; 403 for a failed `verify_inbound`.

### Admin (`get_current_active_superuser`, all)
- `GET /admin/server-channels/channel-types` → `list[ChannelTypePublic]` — registered adapters, for the type picker
- `GET /admin/server-channels/auto-install-list` / `POST .../auto-install-list` (`AutoInstallBundleAdd`) / `DELETE .../auto-install-list/{bundle_uuid}` — declared **before** `/{channel_id}` routes so the literal path wins FastAPI matching
- `GET /admin/server-channels` → `list[ServerChannelPublic]`
- `POST /admin/server-channels` (`ServerChannelCreate`) → `ServerChannelPublic`
- `PUT /admin/server-channels/{channel_id}` (`ServerChannelUpdate`) → `ServerChannelPublic`
- `DELETE /admin/server-channels/{channel_id}` → 204 (bindings cascade)
- `GET /admin/server-channels/{channel_id}/setup-instructions` → `ChannelSetupInstructions`
- Debug-panel and recent-sender routes — see [Channel Debug Monitor tech](channel_debug_monitor_tech.md)
- `POST /admin/server-channels/{channel_id}/test-outbound` (`ChannelTestOutboundRequest`) → `ChannelTestOutboundResult` — always 200; failure travels in `success=false` + `error`, mirroring the mail-server "test connection" pattern, since this endpoint's whole job is surfacing the failure reason

Every admin mutation writes a `SecurityEvent` (`_audit` helper in the route file); update's audit `details` carry field *names* only, never values (the write path can include the secret).

## Services & Key Methods

### `ServerChannelService` (`server_channel_service.py`)
- `get_by_webhook_token(session, token)` — **enabled-only** lookup; this is what makes a disabled channel 404 identically to an unknown token
- `create_channel` / `update_channel` / `delete_channel` — `update_channel` pops non-column fields (`secrets`, `regenerate_webhook_token`) before the generic `sqlmodel_update`, only overwrites `encrypted_secrets` when a non-empty `secrets` value is supplied, and invalidates the adapter's cached outbound token on secret rotation
- `webhook_url(channel)` — built from `settings.webhook_base_url` (an alias of `settings.backend_base_url`; see Configuration), never from the inbound request, so the URL shown to an admin is correct regardless of which host they hit the admin UI from
- `list_recent_senders` / `resolve_test_thread_key` — test-send targeting; see [Channel Debug Monitor tech](channel_debug_monitor_tech.md)
- `list_auto_install_bundles` / `add_auto_install_bundle` / `remove_auto_install_bundle` — the joined `AutoInstallBundlePublic` projection resolves `has_trigger_prompt` from each bundle's latest revision in one batched query

### `ChannelDebugBuffer` (`channel_debug_buffer.py`)

In-memory, process-local capture of recent channel traffic, feeding the admin debug panel — the buffer itself is never persisted; it dies with the process. That is no longer the whole story for message text at the feature level: the [Auto Routing Tuning](../routing_tuning/routing_tuning_tech.md) feature durably persists a `routing_decision` row per routed message (clamped, TTL'd, superuser-only), and each debug-buffer event links to it via `detail.trace_id` when one was written. `ROUTING_TRACE_STORE_MESSAGE_TEXT` gates that persisted row's exposure of sender text by allowlist rather than by naming the fields that carry it: with it off, the stored/served `stages` payload is projected down to fields explicitly declared free of the sender's words (on both the write path and the read path, from one shared definition), so a field not yet vouched for is withheld by default — including one added after the gate was written — rather than exposed by default until someone remembers to add it to a list. A couple of candidate fields are deliberately admitted despite the gate because they are the *agent owner's* configuration rather than sender-derived. The gate hides, it does not erase: rows keep their data until `ROUTING_TRACE_RETENTION_DAYS` expires them or an admin clears the traces. Documented separately: [Channel Debug Monitor](channel_debug_monitor.md) / [tech](channel_debug_monitor_tech.md).

### `ChannelRoutingService` (`channel_routing_service.py`)

Both routing passes live here, not on `ChannelInboundService` — [Auto Routing Tuning](../routing_tuning/routing_tuning_tech.md)'s Phase 3 split the *decision* away from its *effects* so that `POST /admin/routing/simulate` could reuse the real routing code without a `simulate=True` flag threaded through the pipeline. The guarantee is structural rather than conditional: simulate cannot bind, install or reply because nothing reachable from `decide()` can. `tests/architecture/channel_routing_purity_test.py` fails if an effectful import appears in this module.

- `decide(*, user_id, text, include_catalog=True, origin, channel_id, thread_key, actor_user_id)` — the entry point, and the whole public surface for "which agent or bundle should this message go to". Returns a plain-data `RoutingDecisionResult` (ids and trace recorders, never ORM instances bound to a session that is already closed). Takes no `Session`: it cannot touch a transaction the caller is holding. Pass 2 runs only when Pass 1 found nothing **and** `include_catalog` is set
- `_route_installed` (Pass 1) — builds candidates via `ChannelCandidateProvider.build(db, user.id)` (`select(Agent).where(Agent.owner_id == user_id)`; eligible = non-blank `router_trigger_prompt` or non-empty `example_prompts`, else recorded as a `SKIP_NO_TRIGGER_PROMPT` skip), then either short-circuits to the sole eligible candidate (`match_method="only_one"`, only when `_catalog_ballot` shows Pass 2 has nothing to offer) or calls `AgentClassifier.classify` directly. No `AppMCPRoutingService` / `AppAgentRouteService` import anywhere in this module or `channel_candidate_provider.py` — enforced by `tests/architecture/channel_routing_scope_test.py` (AST-based, fails on the import). The `agent.owner_id != user.id` guard is kept as an unreachable-by-construction postcondition assertion.
- `_route_catalog` (Pass 2) — builds the candidate list from `ServerAutoInstallBundle`, filtering on: not already installed by this user, `CatalogService.user_can_install(db, bundle, user)`, and a non-empty `router_trigger_prompt` on the latest revision; classifies via `AgentClassifier.classify` directly (not `AIFunctionsService.route_to_agent` — routing_tuning's Phase 5 collapsed all four routing consumers onto one classifier)
- `run_in_thread` / `_route_installed_in_thread` / `_route_catalog_in_thread` — offload both passes' blocking LLM HTTP calls via `anyio.to_thread.run_sync`; the thread targets open their **own** DB session and return plain ids, never closing over the caller's session (a cancelled task can't interrupt a running thread). `run_in_thread` is deliberately public: `channel_inbound_service` and `routing_tuning_service` both call it for their trace writes

### `ChannelInboundService` (`channel_inbound_service.py`)
- `handle_inbound(db, webhook_token, request, body)` — the entry point; steps 1–5 (channel resolve, verify, dedup, whitelist, user resolution) run inline and cheap; everything from binding dispatch onward for a **new thread** is scheduled as a background task (`_route_new_thread`) so the webhook can ack immediately — most channels retry a non-2xx response forever, so a slow synchronous reply is treated as a correctness risk, not just a latency one
- `_route_new_thread` — the background task: calls `ChannelRoutingService.decide(...)`, then acts on the answer (bind, install, ingest, reply). Routing itself is not implemented here; see `ChannelRoutingService` above
- `_upsert_binding` — the race guard: inserts, and on `IntegrityError` (the unique `(server_channel_id, thread_key)` constraint) rolls back and re-reads the winner's row, returning `(binding, created=False)`
- `_handle_lost_race` / the synchronous ownership check in `handle_inbound` — both branches (an *already-bound* thread receiving a message from a different sender, and a *newly-bound* thread where this caller lost the creation race to someone else) independently re-verify `binding.user_id == sender_user_id` before doing anything with the message
- `flush_pending_bindings(db)` — scheduler/test entry point; iterates all `pending_install` bindings, each in its own try/except; delegates per-row to `_flush_one`
- `_flush_one` — the `critical_state` divergence lives here: `status in {"error", "deprecated"}` fails the binding, `critical_state` does not (falls through to "still waiting"); a binding stuck for more than `_PENDING_MAX_AGE_SECONDS` (30 min) fails regardless of status, so a wedged build doesn't wait forever
- `_ingest` — the actual hand-off to `ChannelIngestionService.ingest_inbound_message`, resuming by `binding.session_id` when the row still exists, otherwise creating fresh (stamping `session_metadata_extra` with `server_channel_id`, `thread_key`, `sender_external_id` on first creation only)
- Two bounded, TTL-swept, process-local in-memory dicts guard against unbounded growth from attacker-supplied keys: `_recent_message_ids` (pre-binding redelivery dedup) and `_recent_security_events` (throttles repeated denial/verification-failure audit writes to one per 5 minutes per key)

### `ChannelOutboundService` (`channel_outbound_service.py`)
- `handle_stream_completed` / `handle_stream_error` — registered as event-bus subscribers in `app/main.py`; first move in both is a cheap `session.integration_type.startswith("channel_")` gate, since they fire for every stream on the instance
- `notify_progress` — no-op when `adapter.capabilities.supports_progress_updates` is false, so callers in the inbound pipeline never need to branch on transport capability
- `_deliver` — on failure, records `last_error` on the binding via `_record_error`, which refuses to overwrite an existing `failed` binding's diagnosis with a lesser "and we couldn't tell them either"

### `GoogleChatAdapter` (`adapters/google_chat.py`)
- `verify_inbound` — extracts the bearer token, calls `verify_google_signed_jwt(token, audience=project_number, issuers=[chat@system.gserviceaccount.com], certs_url=<Chat JWKS URL>)`; a `GoogleCertsUnavailable` is caught and re-raised as `ChannelVerificationError` (denies, but is logged distinctly from a bad signature); parses `MESSAGE` / `ADDED_TO_SPACE` events, drops everything else (including the bot's own posts, identified via `sender.type != "HUMAN"`) as `event_kind="ignored"` — deliberately acked rather than errored, since Chat retries a non-2xx event forever
- `send_message` — chunks text at 4096 chars (preferring a newline split point), mints/caches a `chat.bot`-scope access token per channel via a JWT-bearer grant (implemented with PyJWT rather than adding `google-auth`), retries 3 times with short backoff, gives up with `ChannelSendError` on a non-429 4xx
- `get_setup_instructions` — returns the ordered admin checklist and non-secret details (audience, bot scope) shown in `ChannelSetupInstructionsPanel`
- `build_sync_response` — renders `{"text": ...}`, which Chat displays as an in-thread reply from the webhook's own HTTP response — this is what makes a denial reply possible before any outbound credential exists

## Frontend Components

- `ServerChannelsCard` — `useQuery(["serverChannels"])`; per-row amber badges for "no allowed senders" (empty whitelist, computed via `parseWhitelist`) and "no credential" (`!has_outbound_credentials`); enable/disable is an inline mutation scoped to that row
- `ServerChannelDialog` — two steps, mirroring "Add Credential": pick the type from a card list (`ChannelTypePicker`, fed by `useQuery(["serverChannelTypes"])`), then configure it. The picked type stays visible in step 2 with a "Change" action on create, and reads "Type can't be changed after creation" on edit. Editing opens straight on step 2
- `ServerChannelForm` — react-hook-form + zod, remounted per type (keyed), so its defaults and schema are built once from `getChannelTypeMeta(channelType)`. Config inputs, their validation and the secrets-field copy all come from that registry entry; a type with no entry gets a raw JSON config textarea (validated as a JSON object) rather than another type's fields. Shows `whitelist.isEmpty` / `whitelist.hasWildcard` warnings live as the admin types, using the same tokenizing `parseWhitelist` the list card uses
- `channelTypes.ts` — the frontend half of the adapter registry: icon, tagline, name placeholder, config-field descriptors (label, placeholder, help, validation regex) and secrets-field copy per `channel_type`
- `ChannelSetupInstructionsPanel` — `useQuery(["serverChannelSetup", channelId])`; copy-to-clipboard webhook URL; renders adapter-supplied `details`/`steps` generically; "Test outbound" posts to `test-outbound` and renders `success`/`error` inline (not just as a toast, since the diagnostic reason is the point); "Regenerate webhook token" behind a destructive confirm dialog
- `ChannelDebugDialog` and the `ChannelSetupInstructionsPanel` test-send target picker — see [Channel Debug Monitor tech](channel_debug_monitor_tech.md)
- `AutoInstallAgentsCard` — `useQuery(["autoInstallBundles"])` + reuses the existing `["catalog"]` query for the add-picker; per-entry amber badges for non-public visibility and missing trigger prompt, each with a tooltip stating the specific reason

## Configuration

- `SERVER_CHANNEL_WEBHOOK_MAX_BODY_BYTES` (`backend/app/core/config.py`, default `262_144` / 256 KiB) — checked against both `Content-Length` (fast-fail) and the actual read body (since `Content-Length` can lie or be absent under chunked transfer)
- `SERVER_CHANNEL_WEBHOOK_RATE_LIMIT_PER_MIN` (default `120`) — per-webhook-token, via the shared process-local `RateLimiter`
- `SERVER_CHANNEL_DEBUG_BUFFER_SIZE` / `SERVER_CHANNEL_DEBUG_TEXT_MAX_CHARS` — see [Channel Debug Monitor tech](channel_debug_monitor_tech.md)
- `BACKEND_BASE_URL` (`backend/app/core/config.py`, default empty) — the publicly reachable origin of the **backend**, resolved once in `settings.backend_base_url` and used by every absolute API URL handed outside the platform: the webhook URL an admin pastes into Google Chat, task-trigger `/api/v1/hooks/...` and agent-hook `/agent-hooks/...` URLs, the consumer-facing Agent REST API base (`agent_api_token_service.build_base_url`), signed A2A attachment links (`a2a_event_mapper`), and the RFC 8414 desktop/app-auth discovery endpoints in `backend/app/main.py`. Empty falls back to `FRONTEND_HOST`. It is a separate setting because a real deployment serves the SPA on `dashboard.example.com` and the API on `api.example.com`, and a URL pointing at the SPA origin 404s. `WEBHOOK_BASE_URL` is the former name — introduced when only webhooks were affected — and is still honoured as a fallback; `BACKEND_BASE_URL` wins when both are set. `settings.webhook_base_url` remains as an alias.
- Fernet encryption key and `FRONTEND_HOST` are pre-existing platform settings this feature reuses

### Local testing against a real Google Chat app

Google Chat only posts to a public HTTPS endpoint, so a local backend needs a tunnel:

1. `make webhook-tunnel` — opens a pinggy HTTPS tunnel to `localhost:8000` (leave it running).
2. `make webhook-set-url URL=https://<tunnel-host>` — writes `BACKEND_BASE_URL` into `.env`, recreates the backend container and health-checks the tunnel.
3. Reopen the channel's setup panel in Admin → Server Configuration → Channels and copy the now-tunnelled webhook URL into the Chat API **Connection settings → HTTPS endpoint URL**.
4. `make webhook-clear-url` when finished, to drop back to `FRONTEND_HOST`.

The tunnel host changes on every restart, so steps 2–3 repeat each session. `project_number` must be the numeric GCP project number of the Chat app (the JWT audience) — a wrong value rejects every inbound event with a 403 and no other symptom.

## Security

- **Verification is the single authentication chokepoint** (`ChannelAdapter.verify_inbound`) and the pipeline enforces that nothing before it in `handle_inbound` has parsed or trusted the payload. Every value on `ChannelInboundMessage` is attacker-influenced except fields the adapter took from *verified* JWT claims.
- **`GoogleCertsUnavailable` deliberately does not subclass `ValueError`.** `verify_google_signed_jwt` catches `(JoseError, ValueError)` to turn Authlib's bare `ValueError` (an unknown `kid`, an oversized header — both attacker-reachable on the cheapest possible probe) into "invalid token." If `GoogleCertsUnavailable` were a `ValueError`, that same handler would swallow it, and a genuine Google JWKS outage would be misreported as "this signature is invalid" — collapsing exactly the distinction the webhook needs to tell an outage apart from a forgery. This is enforced by a backend test, not just a comment.
- **The hardening was fixed at `_get_google_certs`, not at each call site.** A JWKS response is only cached when it is both a 2xx *and* shaped like a JWKS document (`{"keys": [...]}`) — without both checks, a Google 5xx carrying a JSON error body would be cached for a full hour and reject every verification against it until the cache expired. `verify_google_token` (the pre-existing Google OAuth login path) catches `GoogleCertsUnavailable` and returns `None`, preserving its exact prior external contract; `verify_google_signed_jwt` lets it propagate so the channel webhook can react to "cannot verify" differently from "invalid."
- **Egress guard on every outbound HTTP call** — `assert_url_allowed` wraps both the Chat API send URL and the OAuth token endpoint URL in the adapter, reusing the platform's existing SSRF/egress protection rather than adding a parallel one.
- **Secrets discipline** — `encrypted_secrets` is Fernet-encrypted via the existing `encrypt_field`/`decrypt_field` helpers, is write-only at every layer (no response DTO carries it, `ServerChannelPublic` exposes only `has_outbound_credentials: bool`), and is never logged.
- **`match_email_pattern`** (`services/common/email_patterns.py`) is shared verbatim between the email integration and the channel whitelist: comma-separated `fnmatch` globs, case-insensitive, blank entries ignored, and **fails closed** — an empty or `None` pattern string matches nothing. `"*"` is the only glob that matches everything; because matching is any-token-in-the-list, a whitelist like `"*, ops@corp.com"` is a blanket allow, not a scoped list — both the backend matcher and the frontend's `parseWhitelist` tokenizer treat it identically, so the admin UI's warning can't disagree with the enforced behavior.
- **`SecurityEvent` audit coverage**: channel create/update/delete/token-regenerate (admin actions, un-throttled), verification failure and whitelist denial (throttled to one per 5 minutes per key, since both are attacker-triggerable at will against a valid token), auto-registration, and auto-install. Audit failures never break the request path — every write is wrapped and logged on failure rather than raised.
- **Known audit gap (deferred, not fixed here):** rejection-path events (`_audit`) are attributed to `channel.created_by`. Since that FK is `ON DELETE SET NULL`, deleting the superuser who created a channel silently stops `SecurityEvent` rows being written for that channel's future denials/verification failures — the event still reaches the application log, just not the audited trail.
- **Message-text logging gap — closed.** Pass 1 used to route through `app_agent_router.py` / `app_mcp_routing_service.py`, which logged inbound message text at INFO level — logging that predates this feature and was written for internal App MCP traffic, but through this feature the same code path also carried externally-sourced, non-platform-user text. The Auto Routing Tuning work (Phase 1) downgraded those lines to `debug`; the durable `routing_decision` trace is the intended replacement observability surface (superuser-only, TTL'd — see `docs/plans/auto_routing_tuning_plan.md` §5), not an INFO log line. Since the [channel routing scope split](../../plans/channel_routing_scope_split_plan.md), Pass 1 no longer calls into either App MCP module at all — `channel_routing_service.py` and `channel_candidate_provider.py` do their own message-text logging, deliberately at `debug` throughout, for the same reason.

## Shared Refactors (introduced by this feature, consumed by others)

- `app.services.common.email_patterns.match_email_pattern` — extracted from the email integration's `routing_service.py::_match_email_pattern`; both features now import the same function.
- `app.core.security.verify_google_signed_jwt` — generalized JWKS verifier (issuer(s)/audience/certs-url all parameters) backing both `verify_google_token` (Google OAuth) and `GoogleChatAdapter.verify_inbound`.
- `app.services.users.user_service.UserService.create_external_user` — unifies the email integration's `create_email_user` and channel auto-registration: idempotent, passwordless, get-or-create by normalized email, parameterized on `confirmed` and an audit `provenance` string. Never mutates an existing account.

## Extension Seam

Adding a new channel type is: write a module implementing `ChannelAdapter` (`validate_config`, `verify_inbound`, `send_message`, `get_setup_instructions`, plus the `capabilities` property) and add one entry to `CHANNEL_ADAPTERS` in `adapters/registry.py`. No migration and no pipeline change — `channel_type` is a plain indexed string column, validated against the registry only at create/update time in the service layer, not in the model. The admin UI follows the registry: a newly registered adapter shows up as a card in the type picker immediately and is configurable through a raw JSON config editor without a frontend change. Adding an entry to `frontend/src/components/Admin/ServerChannels/channelTypes.ts` upgrades that to a typed form (labelled, validated config fields + type-specific credential copy) — recommended, but not a prerequisite for registering the adapter.
