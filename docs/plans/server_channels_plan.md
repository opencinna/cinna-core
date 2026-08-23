# Server Channels — Implementation Plan

**Feature name:** `server-channels`
**Status:** Draft for implementation
**First channel:** Google Chat (adapter contract extensible to Discord, Telegram, WhatsApp, email, …)

---

## 1. Overview

Admin-configured, server-wide **Channels** let external people (e.g. company employees) talk to platform agents from outside the platform — starting with Google Chat. Each channel instance owns its transport trust model, an email-pattern whitelist, and an auto-register toggle. Inbound messages are routed to an agent (installed agents first, then a server-wide auto-install catalog pass), bound per-thread to a session, and replies plus progress events flow back out through the channel.

Core capabilities:
- Admin CRUD for channel instances at `/admin/server-configuration` (new "Channels" tab), superuser-only.
- Google Chat adapter: webhook JWT verification, sender email extraction, outbound replies via Chat REST API.
- Email-pattern whitelist (`*@example.com, devops.*@support.com` — fnmatch semantics shared with the email integration).
- Auto-registration of passwordless, email-confirmed users (Google-verified identity).
- Two-pass routing: user's installed agents (existing App MCP router) → server-wide auto-install list (catalog candidates by `router_trigger_prompt`), auto-install on match.
- Strict per-thread → session binding; resumed threads bypass routing entirely.
- Progress feedback ("Installing your agent…", "Thinking…") gated on per-channel capabilities.

```
Google Chat ──webhook──▶ POST /api/v1/channels/{webhook_token}/inbound
                              │ 1. adapter.verify_inbound (Google JWT, fail closed)
                              │ 2. whitelist check (fnmatch email patterns)
                              │ 3. resolve user (auto-register if enabled)
                              │ 4. binding lookup (channel_id, thread_key)
                              │      hit(active) ──▶ ChannelIngestionService.ingest_inbound_message
                              │      hit(pending) ─▶ park message
                              │      miss ─────────▶ Pass 1: AppMCPRoutingService (installed)
                              │                      Pass 2: catalog candidates from server
                              │                              auto-install list → InstallService
                              │                              → binding(pending_install), park msg
                              ▼
                    scheduler flushes pending bindings once env is running
                              │
        STREAM_COMPLETED ─────┴──▶ binding lookup by session_id ──▶ adapter.send_message
```

---

## 2. Architecture Overview

### Components

| Component | Location | Responsibility |
|---|---|---|
| Models | `backend/app/models/server_channels/` | `ServerChannel`, `ServerAutoInstallBundle`, `ChannelThreadBinding` + DTOs |
| Adapter contract + registry | `backend/app/services/server_channels/adapters/` | `ChannelAdapter` protocol, `ChannelInboundMessage`, `ChannelCapabilities`, `CHANNEL_ADAPTERS` registry, `google_chat.py` |
| Admin service | `backend/app/services/server_channels/server_channel_service.py` | Channel CRUD, secret encryption, setup instructions, auto-install list management |
| Inbound pipeline | `backend/app/services/server_channels/channel_inbound_service.py` | verify → whitelist → user → binding → routing → ingest/park |
| Outbound delivery | `backend/app/services/server_channels/channel_outbound_service.py` | `STREAM_COMPLETED`/`STREAM_ERROR` subscribers → adapter send |
| Pending scheduler | `backend/app/services/server_channels/channel_pending_scheduler.py` | Flush `pending_install` bindings when environments reach `running` |
| Routes | `backend/app/api/routes/server_channels.py` | Public webhook + superuser admin CRUD |
| Admin UI | `frontend/src/components/Admin/` + `/admin/server-configuration` | Channels tab: channel list, config dialog, setup instructions, auto-install picker |

### Reused existing systems (do NOT reinvent)

- **`ChannelIngestionService`** (`backend/app/services/sessions/channel_ingestion_service.py`) — the canonical inbound substrate. This feature adds a new `SessionSender` kind (`channel_caller`) and becomes another caller of `ingest_inbound_message`. Never call `MessageService`/`SessionService` primitives directly.
- **Email integration patterns** (`backend/app/services/email/`) — blueprint for: fnmatch whitelist (`routing_service.py` `_match_email_pattern`), idempotent passwordless user creation (`UserService.create_email_user`), auto-install (`InstallService.install_bundle_for_email`), message parking + scheduler retry (`process_pending_emails`), `STREAM_COMPLETED`-gated outbound (`sending_service.py` `handle_stream_completed`).
- **App MCP routing** (`backend/app/services/app_mcp/app_mcp_routing_service.py`) — Pass 1 routing over installed agents; install-time auto-routes from `router_trigger_prompt` mean freshly auto-installed agents route with zero extra wiring.
- **`AIFunctionsService.route_to_agent`** — reused directly for the Pass 2 catalog classification (same candidate shape: `{id, name, trigger_prompt}`).
- **`CatalogService`** (`backend/app/services/bundles/catalog_service.py`) — `user_can_install` visibility semantics gate the Pass 2 candidate set (decision: auto-install respects catalog visibility; the list is NOT an implicit grant).
- **`InstallService.install_bundle`** (`backend/app/services/bundles/install_service.py`) — idempotent programmatic install entry point.
- **Google JWT verification** (`backend/app/core/security.py` `verify_google_token`) — generalize the JWKS fetch/cache for the Google Chat issuer.
- **`ServerConfig` admin pattern** (`backend/app/api/routes/server_config.py`, `/admin/server-configuration` HashTabs page) — admin UI slot and superuser-guard conventions.
- **Fernet encryption** for secrets at rest — same treatment as mail-server credentials and webhook tokens.

---

## 3. Data Models

New model domain: `backend/app/models/server_channels/` (re-export from `models/__init__.py`). Service dir is `server_channels` — deliberately NOT `channels`, which already exists for user app-agent routes.

### 3.1 `server_channel` — one row per configured channel instance

| Field | Type | Constraints / Default | Notes |
|---|---|---|---|
| `id` | UUID | PK | |
| `channel_type` | str | indexed, e.g. `"google_chat"` | Must exist in `CHANNEL_ADAPTERS`; validated at create/update |
| `name` | str(255) | unique | Admin display name |
| `enabled` | bool | default `True` | Disabled channel ⇒ webhook returns 404 |
| `config` | JSON | default `{}` | Non-secret adapter config. Google Chat: `{"project_number": "<GCP project number>"}` (the JWT audience) |
| `encrypted_secrets` | str, nullable | Fernet-encrypted JSON string | Google Chat: the outbound service-account JSON. Write-only: never returned by any GET; `ServerChannelPublic` exposes only `has_outbound_credentials: bool` |
| `email_whitelist` | Text, nullable | | Comma-separated fnmatch patterns. **Null/empty = deny all (fail closed).** `"*"` = allow any verified sender |
| `auto_register_users` | bool | default `False` | |
| `webhook_token` | str(64) | unique, indexed | `secrets.token_urlsafe(32)` generated at create; part of the webhook URL path; regenerable via update action |
| `created_by` | UUID, nullable | FK `user.id` ON DELETE SET NULL | |
| `created_at` / `updated_at` | datetime | | |

DTOs (non-table SQLModel): `ServerChannelCreate`, `ServerChannelUpdate` (all-optional patch; `secrets` field accepted on write, never echoed), `ServerChannelPublic` (adds `has_outbound_credentials`, `webhook_url`), `ChannelSetupInstructions` (adapter-shaped dict: webhook URL, audience, step list).

### 3.2 `server_auto_install_bundle` — server-wide auto-install list

Decision: **server-wide single list** (not per-channel). Availability restriction is a bundle-permissions concern, not an auto-install-list concern.

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK | |
| `bundle_uuid` | UUID | FK `agent_bundle.id` ON DELETE CASCADE, **unique** | |
| `added_by` | UUID, nullable | FK `user.id` ON DELETE SET NULL | |
| `created_at` | datetime | | |

DTO: `AutoInstallBundlePublic` — joined projection: `bundle_uuid`, `bundle_id` (reverse-DNS), `name`, `visibility`, `has_trigger_prompt` (bool — a bundle whose latest revision lacks `router_trigger_prompt` can never match Pass 2; UI shows an amber badge).

### 3.3 `channel_thread_binding` — conversation state (the heart of the feature)

Strict per-thread mapping (decision): `thread_key` is always the channel-native thread identity; for Google Chat, `message.thread.name` (`spaces/AAA/threads/BBB`).

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK | |
| `server_channel_id` | UUID | FK `server_channel.id` ON DELETE CASCADE | |
| `thread_key` | str(512) | | UniqueConstraint (`server_channel_id`, `thread_key`) |
| `user_id` | UUID | FK `user.id` ON DELETE CASCADE, indexed | The resolved platform user (sender) |
| `agent_id` | UUID | FK `agent.id` ON DELETE CASCADE | Uninstall cascades the binding ⇒ next message re-routes (intended self-heal) |
| `session_id` | UUID, nullable | FK `session.id` ON DELETE SET NULL, indexed | Null until active; session deletion ⇒ recreate session on same agent at next message |
| `status` | str(32) | default `"pending_install"` | `pending_install` → `active`; `failed` terminal until superseded |
| `pending_messages` | JSON | default `[]` | List of `{"text", "external_message_id", "received_at"}` parked while env builds |
| `last_external_message_id` | str(255), nullable | | Webhook redelivery dedup |
| `last_error` | Text, nullable | | |
| `created_at` / `updated_at` | datetime | | |

Lifecycle: `pending_install` (install running / env building, messages parked) → `active` (session bound). `failed` (env build failed): the next inbound message **deletes the binding and re-runs routing** (self-heal). Rationale for externalizing the binding instead of a `Session` column (the email approach): the binding exists *before* the session (during install/build) and makes resume routing-free — same principle as App MCP's fixed-agent-per-context resume.

### 3.4 Existing-model touches (no schema changes)

- `SessionSenderKind` (`backend/app/models/sessions/session_sender.py`): add `"channel_caller"` to the Literal + constructor `SessionSender.from_channel(*, channel_type: str, external_user_id: str, platform_user_id: UUID, display_name: str | None)` with `external_id = f"{channel_type}:{external_user_id}"`.
- `get_session_sender` reader: `integration_type.startswith("channel_")` → `kind="channel_caller"`, `external_id` best-effort from `session_metadata`.
- `ChannelIngestionService._verify_resume_sender` / `_select_session_owner_id`: `channel_caller` behaves like `task_executor` — session owner MUST be `sender.platform_user_id` (the external user's own account; never the publisher). Sessions stamp `integration_type="channel_google_chat"` (pattern: `channel_<channel_type>`), `session_metadata_extra={"server_channel_id", "thread_key", "sender_external_id"}`.
- No changes to `User`, `Agent`, `Session` tables.

---

## 4. Security Architecture

**Trust chain (documented, deliberate):** the webhook JWT proves *Google Chat* sent the event (issuer `chat@system.gserviceaccount.com`, RS256 against Google's public JWKS, `aud == config.project_number`); the sender email inside the payload is trusted transitively from Google — the same trust tier the email integration extends to IMAP. Verification is the FIRST step and fails closed; nothing else runs on failure.

- **Webhook endpoint** is platform-unauthenticated; defenses: unguessable `webhook_token` in the path (404 on unknown/disabled — no existence leak), adapter verification (403 on bad/missing JWT), rate limiting (per-token, reuse the api-proxy limiter pattern), body size cap.
- **Secrets**: `encrypted_secrets` Fernet-encrypted via existing `backend/app/core/security.py` helpers; write-only (re-enter to rotate); never logged; never in any response DTO.
- **Whitelist**: fail closed (null = deny all). Single shared matcher (see §11 refactors) — identical semantics to the email integration's `auto_approve_email_pattern`. Denials get a static polite reply via the synchronous webhook response (no outbound creds needed) + a throttled `SecurityEvent`.
- **Auto-registered users**: ordinary `agent-user` accounts — passwordless (`hashed_password=None`, like `AuthService.create_user_from_google`), `email_confirmed=True` + `email_confirmed_at` (Google verified the address — same justification as Google OAuth signup). Every downstream gate (agent limits, credential isolation, catalog visibility) applies unchanged. If the person later logs in via Google OAuth, the existing by-email auto-link takes over naturally. **Decision:** the channel whitelist is the sole registration gate — `AUTH_WHITELIST_USER_DOMAINS` is NOT re-checked (precedent: `create_email_user`); document this in the admin UI help text.
- **Session isolation**: sessions are owned by the sender's own user; an external caller can only ever reach their own installs. Blast radius of a whitelist mistake = one empty auto-created account.
- **Auto-install authorization (decision)**: the list does NOT bypass catalog visibility. Pass 2 candidates are filtered through `CatalogService.user_can_install(session, bundle, user)`; a non-PUBLIC, ungranted bundle on the list is simply never a candidate for that user. Admin UI shows a visibility badge/warning for such entries.
- **SecurityEvent audit rows**: channel created/updated/deleted/token-regenerated (admin actions), auto-register user created (provenance: channel id), whitelist denial (throttled), verification failure (throttled), auto-install performed.
- **Admin surface**: every admin route uses `get_current_active_superuser`; no role-based partial access.
- **Logging**: never log the raw bearer JWT, service-account JSON, or full webhook payloads at info level; message text never at info level.

---

## 5. Backend Implementation

### 5.1 Adapter contract — `backend/app/services/server_channels/adapters/`

`base.py`:
- `ChannelInboundMessage` (frozen dataclass): `sender_email`, `sender_display_name`, `external_user_id`, `thread_key`, `text`, `external_message_id`, `event_kind` (`"message" | "added_to_space" | "ignored"`), `raw: dict`.
- `ChannelCapabilities` (frozen dataclass): `supports_progress_updates: bool`, `supports_message_edit: bool`, `supports_markdown: bool`, `max_message_chars: int | None`, `supports_sync_reply: bool`.
- `ChannelAdapter` (Protocol/ABC): `channel_type: ClassVar[str]`; `capabilities` property; `validate_config(config: dict) -> None`; `async verify_inbound(request, channel) -> ChannelInboundMessage` (raises `ChannelVerificationError`); `async send_message(channel, thread_key, text) -> str | None` (returns external message id); `async update_message(channel, thread_key, external_message_id, text) -> None` (optional; no-op default); `get_setup_instructions(channel, backend_base_url) -> dict`; `build_sync_response(text) -> dict | None` (payload to return in the webhook HTTP response, for channels that support it).
- `registry.py`: `CHANNEL_ADAPTERS: dict[str, ChannelAdapter] = {"google_chat": GoogleChatAdapter()}`; `get_adapter(channel_type)` raising a domain error for unknown types. Adding a channel = new adapter file + one registry entry (no migration).

`google_chat.py`:
- **verify_inbound**: extract `Authorization: Bearer <JWT>`; verify RS256 against Google's public JWKS for `chat@system.gserviceaccount.com` (generalized cached verifier, see §11); require `iss == "chat@system.gserviceaccount.com"` and `aud == channel.config["project_number"]`. Parse event: `MESSAGE` → extract `message.sender.email`, `message.sender.name` (external_user_id), `message.thread.name` (thread_key), text with the bot @-mention stripped via `message.annotations`/`argumentText`; `ADDED_TO_SPACE` → `event_kind="added_to_space"`; anything else (bot's own messages, membership events) → `event_kind="ignored"`.
- **send_message**: Chat REST API `spaces.messages.create` with `messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` and `thread.name = thread_key`; auth via service-account JWT-bearer grant (scope `https://www.googleapis.com/auth/chat.bot`) minted from `encrypted_secrets` — check whether `google-auth` is already a backend dependency; if not, implement the JWT grant with Authlib (already a dependency). Cache the access token per channel until expiry (in-memory, per-process — acceptable). Async httpx; 3 immediate retries with backoff; chunk messages over `max_message_chars` (4096).
- **capabilities**: progress updates ✓, message edit ✓ (`spaces.messages.patch` — MVP may ship `update_message` as no-op and post new messages), markdown limited (Chat's formatting subset), sync reply ✓.

### 5.2 `ServerChannelService` — `server_channel_service.py`

- `list_channels(session) -> list[ServerChannel]`; `get_channel(session, id)`; `get_by_webhook_token(session, token) -> ServerChannel | None` (enabled-only).
- `create_channel(session, data, user) -> ServerChannel` — validates type via registry + `adapter.validate_config`; encrypts secrets; generates `webhook_token`.
- `update_channel(session, channel, data, user)` — secrets only overwritten when a non-empty value is supplied; `regenerate_webhook_token` as an explicit flag on the update DTO.
- `delete_channel(session, channel)` — cascades bindings.
- `get_setup_instructions(channel) -> ChannelSetupInstructions` — delegates to adapter; webhook URL built from `settings` backend host.
- Auto-install list: `list_auto_install_bundles(session)` (joined projection), `add_auto_install_bundle(session, bundle_uuid, user)` (validates bundle exists + has `latest_revision_id`), `remove_auto_install_bundle(session, bundle_uuid)`.

### 5.3 `ChannelInboundService` — `channel_inbound_service.py` (the pipeline)

`async handle_inbound(webhook_token: str, request: Request) -> dict | None` (return value = sync-response body):

1. **Resolve channel** by token (enabled) → 404 if missing.
2. **Verify** via adapter → 403 on `ChannelVerificationError`. `event_kind="ignored"` → ack `{}`. `added_to_space` → static sync welcome reply.
3. **Dedup**: if `external_message_id == binding.last_external_message_id` for an existing binding → ack (Google redelivers on slow ack).
4. **Whitelist** via shared matcher → miss: sync denial reply + throttled SecurityEvent, ack 200.
5. **Resolve user** by lowercased email → missing: if `auto_register_users` → `UserService.create_external_user(...)` (see §11; passwordless, confirmed, SecurityEvent with channel provenance); else sync denial reply.
6. **Binding lookup** `(server_channel_id, thread_key)`:
   - `active`: build `SessionSender.from_channel(...)`; call `ChannelIngestionService.ingest_inbound_message` with `thread_key=str(binding.session_id)` (session-id resolve, the A2A/App-MCP idiom), `integration_type="channel_google_chat"`, access policy `expected_owner_id=binding.user_id`. If `session_id` is null (session was deleted) → create fresh session on the bound agent, restamp. Return sync ack `None` (reply arrives async via outbound path).
   - `pending_install`: append to `pending_messages`, sync reply "Still setting up your agent — I'll answer shortly."
   - `failed`: delete binding, fall through to routing (self-heal).
   - missing → step 7.
7. **Pass 1 — installed agents**: `AppMCPRoutingService.route_message(db, user_id, message, ...)` (reuse as-is; install-time auto-routes make new installs immediately routable; note for the developer: confirm the `channel` parameter value — reuse the `app_mcp` effective-route set rather than adding a new route-channel flag in MVP). On match → create binding (`active`), create session + ingest, sync ack "Working on it…" if capabilities allow.
8. **Pass 2 — catalog fallback** (decision: runs on ANY Pass 1 miss, including users who have installs that just didn't match): candidates = auto-install list bundles that (a) the user hasn't installed, (b) pass `CatalogService.user_can_install`, (c) have a non-empty `latest_revision.router_trigger_prompt`. Shape `[{id: bundle_uuid, name, trigger_prompt}]` → `AIFunctionsService.route_to_agent(message, candidates)`. On match → `InstallService.install_bundle(session, user, bundle)` (idempotent), create binding `pending_install` with the message parked, sync reply "Setting up **<agent name>** for you — first-time setup takes a few minutes."
9. **No match** → sync reply "I couldn't find an agent that can help with that. Contact your admin."

Webhook route acks fast: steps 1–5 inline (cheap), steps 6–9 scheduled as a background task when they involve install/session work; the sync response carries only the static acknowledgement. Binding-creation race: rely on the unique constraint + `IntegrityError` catch-and-reread (the `ServerConfigService.get_or_create` idiom).

`async flush_pending_bindings(session)` (scheduler entry): for each `pending_install` binding, check the install's environment status — `running` → create session via `ingest_inbound_message` with the first parked message, then feed remaining parked messages in order, set `active`, stamp `session_id`, notify "Your agent is ready." Env `error`/critical → set `failed`, `last_error`, notify failure.

### 5.4 `ChannelOutboundService` — `channel_outbound_service.py`

- Subscribe to `STREAM_COMPLETED` (mirror email's `sending_service.handle_stream_completed` registration site): cheap gate `session.integration_type.startswith("channel_")` → binding lookup by `session_id` → fetch the final assistant message text → `adapter.send_message(channel, binding.thread_key, text)`. Also subscribe to `STREAM_ERROR` → short error notice.
- All outbound HTTP is async httpx (event handlers are asyncio tasks on the main loop — never block; if any sync I/O is unavoidable, offload via `anyio.to_thread`).
- MVP delivery is best-effort (3 immediate retries, then `last_error` on the binding + warning log). A persistent outbound queue (email's `OutgoingEmailQueue` pattern) is a listed future enhancement.
- Progress notifications: `notify_progress(channel, binding, text)` helper used by the pipeline (routing started / installing / ready / failed), no-op when `capabilities.supports_progress_updates` is false.

### 5.5 Scheduler — `channel_pending_scheduler.py`

Interval loop (30–60s), started alongside the email schedulers in the app lifespan; **gated by the `TESTING` flag** per project scheduler conventions (tests invoke `flush_pending_bindings` directly). Single query for `pending_install` bindings; per-binding try/except so one failure never starves the rest. No advisory-lock leader election needed at current deployment scale (single backend process) — note in code if multi-worker deployment arrives, mirror the model-discovery leader pattern *including its documented connection-pool lock caveat*.

### 5.6 API Routes — `backend/app/api/routes/server_channels.py` (register in `api/main.py`, tags=`["server-channels"]`)

**Public (no auth dependency):**
- `POST /api/v1/channels/{webhook_token}/inbound` → `ChannelInboundService.handle_inbound`; response is the adapter sync payload or `{}`; rate-limited.

**Admin (all `get_current_active_superuser`):**
- `GET /api/v1/admin/server-channels` → `list[ServerChannelPublic]`
- `POST /api/v1/admin/server-channels` → create (`ServerChannelCreate`)
- `PUT /api/v1/admin/server-channels/{id}` / `DELETE .../{id}`
- `GET /api/v1/admin/server-channels/{id}/setup-instructions` → `ChannelSetupInstructions`
- `POST /api/v1/admin/server-channels/{id}/test-outbound` → sends a test message to a caller-supplied `thread_key`/space (validates the service-account credential; mirrors mail-server "test connection")
- `GET /api/v1/admin/server-channels/auto-install-list` / `POST .../auto-install-list` (`{bundle_uuid}`) / `DELETE .../auto-install-list/{bundle_uuid}` — **declare these BEFORE the `/{id}` routes** so the literal path wins FastAPI matching.

Errors: domain exceptions in the service (`ChannelVerificationError`, `ChannelAccessDenied`, `UnknownChannelTypeError`) translated to HTTPException at the route layer, per backend conventions.

---

## 6. Frontend Implementation

### 6.1 Admin page — extend `/admin/server-configuration`

`frontend/src/routes/_layout/admin/server-configuration.tsx`: add HashTabs entry `{ value: "channels", title: "Channels" }` (existing pattern: one tab per concern; disclaimer keeps its "interface" tab). The tab renders a 2-column grid:

1. **`ServerChannelsCard`** (`frontend/src/components/Admin/ServerChannelsCard.tsx`) — list of channel instances: name, type badge, enabled switch (inline mutation), edit/delete actions, empty state ("No channels configured. Add Google Chat to let your team talk to agents."), "Add Channel" button. Modeled on `MailServerSettings` card structure.
2. **`AutoInstallAgentsCard`** (`frontend/src/components/Admin/AutoInstallAgentsCard.tsx`) — the server-wide auto-install list: rows with bundle name, reverse-DNS id, visibility badge (amber warning when not PUBLIC: "not installable by external users until made public/granted"; amber when missing a router trigger prompt: "will never match routing"), remove button; add-picker populated from the catalog listing (`CatalogService` client) filtered to installable bundles.

**`ServerChannelDialog`** (create/edit, react-hook-form + zod, modeled on `ManagedCredentialDialog`):
- Type select (only "Google Chat" for now), name, enabled.
- Google Chat config: project number (text, required, digits).
- Service-account JSON: textarea, write-only — on edit shows "•••• credential saved" placeholder; leaving it untouched keeps the stored secret; pasting overwrites.
- Email whitelist: textarea with monospace font + help text explaining fnmatch patterns and **fail-closed** behavior (`empty = nobody`; `* = anyone Google verifies`).
- Auto-register switch with explanatory copy ("creates a passwordless account for whitelisted senders on first contact").

**`ChannelSetupInstructionsPanel`** — shown after create / from an "Setup" action: webhook URL with copy-to-clipboard (existing copy-button component), audience/project-number reminder, ordered steps for the Google Chat app configuration page, "Regenerate webhook token" action with confirm dialog (warns it breaks the existing Google-side config).

### 6.2 State management

- Query keys: `["serverChannels"]`, `["serverChannelSetup", channelId]`, `["autoInstallBundles"]`, picker reuses the existing catalog query key.
- Mutations invalidate `["serverChannels"]` / `["autoInstallBundles"]`; toasts on success/error (sonner).
- All types from the regenerated `@/client` — run `bash scripts/generate-client.sh` after backend routes land.

### 6.3 User flows

- **Admin setup**: Admin → Server Configuration → Channels → Add Channel → fill dialog → save → setup panel shows webhook URL → paste into Google Chat app config → "Test outbound" → done.
- **Employee first contact** (external, no UI): DM the bot → sync ack → (auto-register + routing + install happen server-side, progress messages arrive in-thread) → agent reply lands in the thread; subsequent messages in the thread go straight to the same session.
- Loading/error states per card via React Query `isLoading`/`isError` — render explicit error states, never a lying empty state.

---

## 7. Database Migrations

One Alembic migration: `add_server_channels_tables` (via `make migration`, then review).

- Create `server_channel` (unique: `name`, `webhook_token`; index: `webhook_token`, `channel_type`).
- Create `server_auto_install_bundle` (unique + FK CASCADE on `bundle_uuid` → `agent_bundle.id`; FK SET NULL `added_by` → `user.id`).
- Create `channel_thread_binding` (UniqueConstraint (`server_channel_id`, `thread_key`); indexes: `session_id`, `user_id`; FKs: `server_channel_id` CASCADE, `user_id` CASCADE, `agent_id` CASCADE, `session_id` SET NULL).
- Downgrade: drop the three tables (binding first, then list, then channel).

No changes to existing tables.

---

## 8. Error Handling & Edge Cases

| Scenario | Behavior |
|---|---|
| Unknown/disabled webhook token | 404, no body detail |
| JWT verification failure | 403 + throttled SecurityEvent; never processes payload |
| Whitelist miss | Sync polite denial, 200 ack, throttled SecurityEvent |
| User exists but `is_active=False` | Treated as denial (same reply as whitelist miss) |
| Auto-register off + unknown sender | Sync denial reply |
| Env build fails during `pending_install` | Binding → `failed` + `last_error`, failure notice to thread; next message deletes binding and re-routes |
| Agent uninstalled | FK CASCADE deletes binding; next message re-routes (may auto-reinstall — idempotent App Data reattach makes this safe) |
| Session deleted | `session_id` SET NULL; next message creates a fresh session on the same bound agent |
| Bundle removed from auto-install list | Existing installs/bindings untouched; only future Pass 2 affected |
| Google webhook redelivery | Dedup via `last_external_message_id` |
| Two first-messages race in one new thread | Unique constraint + IntegrityError catch-and-reread |
| Pass 2 picks a bundle whose install fails | Binding `failed` + notice; SecurityEvent-free (operational, not security) but `last_error` populated |
| Agent reply exceeds Chat message limit | Adapter chunks at `max_message_chars` |
| Outbound send fails after retries | `last_error` on binding, warning log — user can re-ask; persistent queue is a future enhancement |
| Sender email missing from event (non-Workspace user) | Treated as verification failure (denial) |
| `AIFunctionsService` cascade fully unavailable | Pass 1 falls back to pattern/single-route paths; Pass 2 returns no-match reply |

---

## 9. UI/UX Considerations

- Channel rows: green dot (enabled) / grey (disabled); type badge; relative "created" timestamp.
- Auto-install rows: amber `visibility` and `no trigger prompt` badges with tooltips explaining exactly why the bundle won't auto-install.
- Whitelist textarea help text mirrors the email integration's pattern docs; explicitly states fail-closed semantics.
- Setup panel: copy-to-clipboard on webhook URL; regeneration behind a destructive-styled confirm.
- All admin copy assumes a superuser reader; no onboarding flow needed.
- In-thread progress messages are short, emoji-light, and only sent when the adapter supports progress updates.

---

## 10. Integration Points

- `ChannelIngestionService` + `SessionSender` — new `channel_caller` kind; the architecture contract test (`backend/tests/architecture/channel_ingestion_callers_test.py`) counts callers per method — `ingest_inbound_message` gains a caller, no threshold issue.
- `AppMCPRoutingService.route_message` — Pass 1 consumer.
- `AIFunctionsService.route_to_agent` — Pass 2 classifier (second caller of the same function the App MCP router uses).
- `InstallService.install_bundle` — Pass 2 install; install-time `_auto_create_app_mcp_route` makes the new install Pass-1-routable for all future threads.
- `CatalogService.user_can_install` — Pass 2 visibility gate.
- `UserService` — new shared `create_external_user` (see §11).
- Event bus — `STREAM_COMPLETED` / `STREAM_ERROR` subscribers, registered where the email integration registers its handlers.
- `ServerConfig` admin page — new HashTabs tab; `AdminMenu` needs no change (page already linked).
- **Client regeneration**: `bash scripts/generate-client.sh` after backend routes land.
- Nginx: `/api/` is already proxied — the webhook path needs no new location block; note the webhook URL in setup instructions must use the public host (`FRONTEND_HOST`-derived), not localhost.

## 11. Shared Refactors (small, each earns its keep — implement as part of this feature)

1. **Email-pattern matcher**: extract the fnmatch logic from `backend/app/services/email/routing_service.py` (`_match_email_pattern`) into a shared util (e.g. `backend/app/utils.py` or `services/common`), consumed by both email routing and the channel whitelist. One source of truth for pattern semantics.
2. **Google JWKS verifier**: generalize `verify_google_token`'s JWKS fetch/cache in `backend/app/core/security.py` into a parameterized helper (certs URL, issuer(s), audience) used by both Google OAuth and the Google Chat adapter.
3. **`UserService.create_external_user(*, session, email, confirmed: bool, provenance: str)`**: unify `create_email_user` and channel auto-registration (both idempotent, passwordless, whitelist-exempt get-or-creates; differ only in `email_confirmed` and audit provenance). Migrate the email path to it.
4. Pass-2 catalog routing deliberately lives in `ChannelInboundService`, NOT in `AppMCPRoutingService` — per the codebase's own "≥ 2 callers" rule; move it only when a second consumer appears.

## 12. Future Enhancements (Out of Scope)

- Additional adapters: Discord, Telegram, WhatsApp, email-as-channel (identity-linking table + verification flow for channels without verified email).
- Multi-user rooms: participant dimension on `channel_thread_binding` + owner-approval flow ("allow X to talk to your agent") + `assert_access` participant check.
- Persistent outbound delivery queue (email's `OutgoingEmailQueue` pattern) with retry scheduler.
- Live "Thinking…" message-edit streaming (post-then-patch) via `update_message`.
- Per-channel auto-install list overrides (schema already isolates the list — adding a nullable `server_channel_id` later is a small migration).
- Slash-command support in channel threads (`/reset` to break a binding and start fresh).
- Out-of-process channel plugins (the `ChannelAdapter` protocol is the seam to remote).

---

## 13. Summary Checklist

### Backend
- [ ] Models: `server_channel`, `server_auto_install_bundle`, `channel_thread_binding` + DTOs in `backend/app/models/server_channels/`; re-export in `models/__init__.py`
- [ ] Migration `add_server_channels_tables` (tables, uniques, indexes, FKs per §7)
- [ ] `SessionSender`: add `channel_caller` kind, `from_channel` constructor, reader mapping for `channel_*` integration types
- [ ] `ChannelIngestionService`: `channel_caller` handling in `_verify_resume_sender` + `_select_session_owner_id` (owner = sender)
- [ ] Adapter contract + registry (`adapters/base.py`, `adapters/registry.py`)
- [ ] `GoogleChatAdapter` (`adapters/google_chat.py`): verify_inbound, send_message (chunking, retries, token cache), setup instructions, sync responses
- [ ] Shared refactors: email-pattern util, generalized Google JWKS verifier, `UserService.create_external_user` (migrate email path)
- [ ] `ServerChannelService`: CRUD + secrets encryption + webhook-token lifecycle + auto-install list management
- [ ] `ChannelInboundService`: full pipeline (§5.3) incl. dedup, race handling, Pass 1/Pass 2 routing, parking
- [ ] `ChannelOutboundService`: STREAM_COMPLETED/STREAM_ERROR subscribers + progress helper
- [ ] `channel_pending_scheduler`: TESTING-gated interval flush
- [ ] Routes: public webhook + admin CRUD + auto-install list (literal paths before `/{id}`); register router; SecurityEvent audit rows
- [ ] Rate limiting on the webhook route

### Frontend
- [ ] Regenerate client (`bash scripts/generate-client.sh`)
- [ ] Channels tab on `/admin/server-configuration` (HashTabs entry)
- [ ] `ServerChannelsCard` + `ServerChannelDialog` (write-only secret field, whitelist help text)
- [ ] `ChannelSetupInstructionsPanel` (copy-to-clipboard, token regeneration confirm)
- [ ] `AutoInstallAgentsCard` (visibility + trigger-prompt badges, catalog-backed picker)

### Testing & validation (API-level, per `backend/tests/README.md`; read it before writing tests)
- [ ] Admin CRUD: superuser-only enforcement; secrets never echoed; webhook-token regeneration
- [ ] Webhook: unknown/disabled token → 404; bad JWT → 403 (mock adapter verification); ignored event kinds ack cleanly
- [ ] Whitelist matrix: patterns, fail-closed empty, case-insensitivity, denial reply
- [ ] Auto-register on/off; created user is passwordless + confirmed + `agent-user`; idempotent on repeat
- [ ] Binding: create/resume, dedup, race (unique-constraint path), failed→re-route self-heal, session-deleted recovery
- [ ] Routing: Pass 1 match, Pass 2 candidate filtering (visibility, already-installed, missing trigger prompt), no-match reply
- [ ] Pending flow: parking, `flush_pending_bindings` (direct call, scheduler TESTING-gated), env-failure path
- [ ] Outbound: STREAM_COMPLETED gating by integration type, binding lookup, chunking
- [ ] Verify existing email-integration tests still pass after the shared-util refactors (regression scope: email topic group)
