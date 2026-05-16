# Channel Ingestion Service — Implementation Plan

> **Status:** Draft architectural plan. Internal backend refactor. No new tables, no migrations, no frontend changes.
>
> **Framing.** This plan is the *grounded alternative* to `docs/drafts/unified-channel-adapters_plan.md`, which was rejected as a premature framework (ABCs, registry, new tables, polling scheduler, identity-link table, contributor protocol). That plan tried to standardise the *transport edge*; this plan extracts only the kernel that survives critical scrutiny — a single internal orchestration service that every inbound entry point already wants to call.
>
> **What we are doing.** Introduce two artifacts: a `SessionSender` value type and a `ChannelIngestionService` (three methods). Migrate four existing inbound entry points through the service: A2A, App MCP, web-UI session create, and internal triggers (scheduler + task execution — split between two sender kinds, see §3.1).
>
> **What we are explicitly not doing.** No `ChannelAdapter` ABC, no registry, no `Channel`/`AgentChannelBinding`/`ChannelThread`/`ChannelIdentityLink` tables, no polling scheduler, no `ChannelType` enum, no `services/channels/` namespace, no outbound unification, no email/webhook/webapp/share migration, no frontend changes, no contributor packaging. If the plan starts drifting toward any of those, see §10.
>
> **Length target.** ~500 lines.

---

## 1. Overview

At least four code paths today create or seed an inbound agent session and they reinvent the same three steps:

| Channel | Sender source | Thread resolution | Message injection |
|---------|---------------|-------------------|-------------------|
| A2A | `A2ATokenPayload` → `user_id` + `access_token_id` | `taskId` → `Session.id` + scope guard | `SessionService.send_session_message(...)` |
| App MCP | JWT → `user_id`; `routing_result.identity_owner_id` for identity | `context_id` → `Session.id` + caller-bound `WHERE` | `MessageService.create_message(...)` then `stream_and_collect_response` |
| Web-UI `POST /sessions` | `CurrentUser` or `GuestShareContext` | Always new session | None — first message lands via separate `POST /messages` |
| Internal triggers (scheduler / task exec) | `agent.owner_id` (system trusts itself) | Always new session per fire | `SessionService.send_session_message(session_id=None, agent_id=..., ...)` |

Every path eventually calls `SessionService.create_session` (`backend/app/services/sessions/session_service.py:21`) and most then call `SessionService.send_session_message` (`:1146`). The "before" step — figuring out *who* and *which session* — is duplicated with subtly different access control, metadata stamping, and integration-type strings.

We add two things:

1. **A value type** (`SessionSender`) — names the sender uniformly across channels.
2. **A service** (`ChannelIngestionService`) — four small methods composing the existing primitives into one orchestration point.

Channels become *clients* of this service. Outbound delivery (SSE / SMTP queue / WS / HTTP callback) stays where it is — it is genuinely heterogeneous and we are not unifying it.

### 1.1 The "≥ 2 callers" rule

A method belongs in `ChannelIngestionService` only if **two or more channels** would call it. If a method would have one caller, it stays in that channel's module. This is the rule that prevents framework drift at smaller scale; we apply it to every method in §4.

### 1.2 High-level flow

```
INBOUND ENTRY POINT               ChannelIngestionService                 EXISTING PRIMITIVES
───────────────────               ─────────────────────────              ──────────────────────
A2A handler                       resolve_or_create_session  ──────►     SessionService.create_session
App MCP handler             ─►    assert_access             ──────►     (in-service ownership/scope checks)
Web-UI POST /sessions             ingest_inbound_message    ──────►     SessionService.send_session_message
Scheduler / task exec
```

The service has no state, no instance fields, and no notion of "which adapter". It takes `integration_type: str` from the caller for stamping only.

### 1.3 What "the win" actually is

The win is **one canonical entry point for the two production send paths** (A2A and App MCP — net negative lines, real consolidation) and **one canonical session-resolution helper for the empty-create paths** (web-UI `POST /sessions` and `input_task_service.py:1271` — net neutral lines, but uniform stamping). Scheduler and human-task execution land in between — net neutral lines, but they pick up consistent `integration_type` stamping and the `system_trigger`/`task_executor` split makes the trust model visible in code rather than hidden in `send_session_message`'s `user_id` check.

We are not claiming uniform net-negative across all migrations. The architecture wins on call-graph clarity, consistent stamping, and the structural distinction between human-initiated and cron-fired trust, not on raw line count.

---

## 2. In Scope / Out of Scope

### 2.1 In scope

1. `SessionSender` (dataclass) + reader `get_session_sender(session)` (pure addition).
2. `ChannelIngestionService` with three methods (§4). Pure addition.
3. Migrate **A2A** — `A2ARequestHandler.handle_message_send` / `handle_message_stream` (`backend/app/services/a2a/a2a_request_handler.py:73-98`, `:145-156`, `:326`).
4. Migrate **App MCP** — `AppMCPRequestHandler._resolve_session` and `_handle_inner` (`backend/app/services/app_mcp/app_mcp_request_handler.py:235`, `:281`, `:103-108`).
5. Migrate **web-UI session creation** — `POST /api/v1/sessions/` (`backend/app/api/routes/sessions.py:158`). See §5.3 for the "create-without-message" wrinkle.
6. Migrate **internal triggers** — scheduler (`backend/app/services/agents/agent_schedule_scheduler.py:141`, `:361`, sender kind `system_trigger`) + task-execution (`backend/app/services/tasks/input_task_service.py:803` and `:1271`, sender kind `task_executor`). The two kinds get **different** `assert_access` semantics — see §4.3 and §7.2.

### 2.2 Out of scope

- **Email** — defer (auto-create-user logic, `EmailMessage` audit, thread-id model don't map cleanly without extra design).
- **Webhook** (`agent_webhook_service.py:567`) — defer (script-trigger variant doesn't create a session at all).
- **Webapp chat / guest share** (`webapp_chat_service.py:86`) — defer (one-active-session-per-share, 4-digit code, grant activation are share-specific).
- **Outbound delivery is not touched.** SSE / MCP streaming / WS fan-out / SMTP queue stay in their modules. No `DeliveryService`, no `BatchingStreamCollector`.
- **No new DB tables.** No `Channel`, `AgentChannelBinding`, `ChannelThread`, `ChannelInboundMessage`, `ChannelOutboundQueue`, `ChannelIdentityLink`. No migration in any phase.
- **No ABCs, Protocols, registries, enums.** `SessionSender.kind` is a `Literal[...]`, not an `Enum`. `integration_type` is a `str`.
- **No `services/channels/` namespace.** Framework-shaped — disallowed. See §11 for placement.
- **No frontend changes.** No API contract changes; no client regeneration.
- **No new event types, no new audit storage.** Existing `event_service` and per-channel logs (`AgentScheduleLog`, A2A task store) are sufficient.

### 2.3 Anti-goal checklist

If during implementation any of these appear, **stop and reconsider** — the plan has drifted:

- A `ChannelAdapter` base class, `ChannelType` enum, or `ChannelAdapterRegistry`.
- A new directory under `backend/app/models/channels/` or `backend/app/services/channels/`.
- An `IngestionResult` field that requires per-channel polymorphism.
- A `process_inbound(channel, raw)` signature instead of explicit typed params.
- An attempt to "also generalise outbound delivery" or "also unify polling".
- A method on `ChannelIngestionService` with exactly one caller. The ≥2-callers rule has been violated.

---

## 3. `SessionSender` and Supporting Types

### 3.1 `SessionSender` value type

**Location:** `backend/app/models/sessions/session_sender.py` (re-exported from `app.models.sessions.__init__` and `app.models.__init__`).

```python
# Pseudocode — exact signatures live in the implementation
@dataclass(frozen=True)
class SessionSender:
    kind: Literal[
        "platform_user",   # web-UI authenticated user, owns the session
        "a2a_caller",      # external caller via A2A access token
        "mcp_caller",      # authenticated platform user calling via App MCP
        "webui_user",      # explicit subkind of platform_user when the inbound is the web-UI itself
        "task_executor",   # human-initiated task execution (real user_id from the route layer)
        "system_trigger",  # genuinely sender-less paths: cron schedules, handover-fired sessions
        "anonymous",       # reserved for future webhook/guest-share use
    ]
    external_id: str                    # token sub, user uuid, "task:{id}", "schedule:{id}", "anonymous"
    display_name: str | None
    platform_user_id: UUID | None       # populated when the sender is bound to a real User row
```

Frozen, no methods that mutate state. Two responsibilities only: (a) name the sender; (b) carry the `platform_user_id` that downstream services need.

**Why `task_executor` is separate from `system_trigger`.** Today's `input_task_service.py:803` is reached from an authenticated route — it carries a real human `user_id`, which `send_session_message` already validates against `Session.user_id` at `session_service.py:1250`. If we collapsed it into `system_trigger` and applied a fast-path allow (§4.3), we would widen trust, not make it explicit. Cron-fired schedulers and handover-spawned sessions are different: they have no human caller, so `agent.owner_id` is the only sensible owner and there is nothing to check against. The two kinds get different `assert_access` semantics (§4.3) precisely to preserve this distinction.

### 3.2 Reader

`get_session_sender(session: Session) -> SessionSender`

Lives next to `SessionSender`. Derives the sender from existing `Session` columns *without DB schema change*:

| `Session.integration_type` | Derivation |
|----------------------------|------------|
| `"a2a"` (or any A2A subtype) | `kind="a2a_caller"`, `platform_user_id=session.user_id`, `external_id=str(session.access_token_id or session.user_id)` |
| `"app_mcp"` | `kind="mcp_caller"`, `platform_user_id=session.caller_id`, `external_id=str(session.caller_id)` |
| `"identity_mcp"` | `kind="mcp_caller"`, `platform_user_id=session.identity_caller_id`, `external_id=str(session.identity_caller_id)` |
| `None` (default — web-UI created) | `kind="webui_user"`, `platform_user_id=session.user_id`, `external_id=str(session.user_id)` |
| `"task"` (new — used by human-initiated task execution) | `kind="task_executor"`, `platform_user_id=session.user_id`, `external_id` from `session_metadata["task_id"]` |
| `"schedule"` (new — used by cron-fired sessions) | `kind="system_trigger"`, `platform_user_id=session.user_id` (= `agent.owner_id` by construction), `external_id` from `session_metadata["schedule_id"]` |
| Anything else (email, webhook, webapp) | Best-effort: `kind="platform_user"`, `platform_user_id=session.user_id`. The reader is forward-compatible; we do not need to teach it about every existing channel — only the four we migrate. |

`get_session_sender` is a pure read; it never writes. Use cases: surfacing the sender on `SessionPublic` (future), debugging, structured logging.

### 3.3 Per-channel constructors

Static factory methods on `SessionSender` that each inbound entry point uses to build a typed sender from its own auth context:

```python
SessionSender.from_a2a(token_payload, access_token_id, default_user_id) -> SessionSender
SessionSender.from_app_mcp(caller_user_id, identity_caller_user_id=None) -> SessionSender
SessionSender.from_webui(current_user) -> SessionSender
SessionSender.from_task_execution(task, executing_user_id) -> SessionSender   # task_executor kind
SessionSender.from_system_trigger(schedule=None, *, owner_user_id) -> SessionSender   # system_trigger kind
```

These are constructors only — they never touch the DB. Each is ~5 lines of straight-line code. They exist so callers don't recreate the dataclass argument-by-argument and so the `external_id` convention (`"task:{id}"`, `"schedule:{id}"`) is captured in one place.

`from_task_execution` produces `kind="task_executor"` with `platform_user_id=executing_user_id` (the real human caller from the route). `from_system_trigger` produces `kind="system_trigger"` with `platform_user_id=owner_user_id` (always `agent.owner_id` by construction — the constructor enforces this invariant).

### 3.4 Supporting types

All co-located with `SessionSender`:

- **`ChannelAccessPolicy`** — dataclass with at most five fields: `expected_owner_id: UUID | None` (whose `agent.owner_id` the sender must match for the owner-match check; required for `task_executor` and `webui_user`), `require_owner_match: bool`, `require_access_token_scope: A2ATokenPayload | None`, `require_caller_in_route: bool`, `allow_system_trigger_fastpath: bool` (only `True` for cron-fired paths). The caller picks the policy; the service does not.
- **`IngestionResult`** — `{session: Session, is_new_session: bool, message_id: UUID | None, action: Literal["streaming","pending","queued","command_executed","error"], error: str | None}`. Mirrors `SessionService.send_session_message`'s return shape so migration is a near-textual swap.

No `RawAttachment` ships in the initial service shape — see §4.4. No `ChannelInboundEvent`, no `ResolvedSender`, no `ThreadResolution`. Channels pass typed Python parameters directly.

---

## 4. `ChannelIngestionService` Interface

**Location:** `backend/app/services/sessions/channel_ingestion_service.py`. Re-exported from `app.services.sessions.__init__`.

**Why `services/sessions/` and not `services/channels/`.** Naming matters. A `services/channels/` directory would suggest a framework with adapters; we have neither. The service is a thin orchestration layer over `SessionService` — it lives next to `SessionService` for the same reason `MessageService` does.

**Statelessness.** All methods are `@staticmethod`. Every method takes `db: DBSession` as a parameter. No instance fields, no singleton, no `__init__` doing setup.

**Initial method shape: three methods.** `ingest_inbound_message`, `resolve_or_create_session`, `assert_access`. Attachment handling is deliberately *not* in the service at merge time — see §4.4 for why.

### 4.1 `ingest_inbound_message`

```python
@staticmethod
async def ingest_inbound_message(
    *,
    db: DBSession,
    agent: Agent,
    sender: SessionSender,
    thread_key: UUID | None,           # existing Session.id for resume; None for new session
    content: str,
    file_ids: list[UUID] | None = None,  # already-uploaded FileUpload.ids — pass-through to send_session_message
    integration_type: str,             # "a2a" | "app_mcp" | "identity_mcp" | "schedule" | "task" | "webui"
    access_policy: ChannelAccessPolicy,
    get_fresh_db_session: Callable[[], DBSession],
    backend_base_url: str | None = None,
    answers_to_message_id: UUID | None = None,
) -> IngestionResult:
    ...
```

**Behaviour** (orchestration only — every step delegates):

1. `assert_access(agent, sender, access_policy)` — raises on denial.
2. `(session, created) = resolve_or_create_session(db, agent=agent, sender=sender, thread_key=thread_key, integration_type=integration_type)`.
3. Delegate to `SessionService.send_session_message(session_id=session.id, user_id=session.user_id, content=content, file_ids=file_ids, get_fresh_db_session=get_fresh_db_session, access_token_id=…, integration_type=integration_type if created else None, backend_base_url=backend_base_url, answers_to_message_id=answers_to_message_id)`.
4. Map the returned dict into `IngestionResult` and return.

**Why this method exists.** A2A, App MCP, scheduler-fired sessions, and human-initiated task execution all do the same thing today: resolve-or-create → drop a user message → kick the stream. Four callers → passes the ≥2-callers rule.

**Why web-UI and `input_task_service.py:1271` don't call it.** See §5.3 and §5.4 — both are "create empty session, no message yet" cases. They call `resolve_or_create_session` directly.

### 4.2 `resolve_or_create_session`

```python
@staticmethod
def resolve_or_create_session(
    *,
    db: DBSession,
    agent: Agent,
    sender: SessionSender,
    thread_key: UUID | None,
    integration_type: str,
    extra_session_kwargs: dict[str, Any] | None = None,  # e.g. {"access_token_id": ...}
) -> tuple[Session, bool]:
    ...
```

**Behaviour:**
- If `thread_key` is given: load `Session` by id; verify the existing session matches `sender` (e.g., `session.user_id == sender.platform_user_id` for `webui_user` and `task_executor`, `session.caller_id == sender.platform_user_id` for `mcp_caller`, `session.access_token_id` scope-check for `a2a_caller`). Return `(session, False)`.
- If `thread_key` is `None`: build `SessionCreate(agent_id=agent.id, mode=…)`. Pick `session_owner_id`:
  - `webui_user`: `agent.owner_id` typically, or `sender.platform_user_id` when explicitly different and the access policy allows it (e.g., guest share — see §5.3).
  - `task_executor`: `sender.platform_user_id` (the executing human user — matches today's `input_task_service.py:803` behaviour where `Session.user_id = user_id` from the route).
  - `system_trigger`: `agent.owner_id` (strictly — by invariant, `sender.platform_user_id == agent.owner_id`).
  - `a2a_caller`: `agent.owner_id` (A2A sessions run in the agent's owner's space; caller is stamped via `access_token_id`).
  - `mcp_caller`: `agent.owner_id` for plain `app_mcp`; `identity_owner_id` for `identity_mcp`. (The caller's user id goes to `caller_id` / `identity_caller_id`.)
- Call `SessionService.create_session(db_session=db, user_id=session_owner_id, data=..., integration_type=integration_type, **extra_session_kwargs)`.
- Post-create: stamp `caller_id` / `identity_caller_id` / `session_metadata` per `sender.kind` (this is the existing per-channel code consolidated). Commit.
- Return `(session, True)`.

**Callers (≥2 check):** A2A handler (`is_new_session=True` branch), App MCP handler (regular + identity), web-UI session create, scheduler, task-exec (`:803`), task create-only (`:1271`). Six callers — solidly meets the rule.

### 4.3 `assert_access`

```python
@staticmethod
def assert_access(
    *,
    agent: Agent,
    sender: SessionSender,
    policy: ChannelAccessPolicy,
) -> None:
    """Raise PermissionError / ValueError on denial. Returns None on accept."""
```

**Behaviour (per `sender.kind`):**
- `webui_user`: enforce `agent.owner_id == policy.expected_owner_id == sender.platform_user_id` unless `policy.require_owner_match=False` (e.g., guest-share session creation, where the caller is *not* the owner).
- `task_executor`: enforce `policy.expected_owner_id == sender.platform_user_id`. This is a **real check**, not a fast-path. It replicates the `chat_session.user_id != user_id` guard at `session_service.py:1250` — the existing trust check survives the migration. Per-task ACLs continue to be checked in `input_task_service.verify_agent_access` (called before `ingest_inbound_message`), so this step verifies "the user_id the route handed us matches the sender we built from it" rather than "this user owns this agent".
- `a2a_caller`: when `policy.require_access_token_scope` is set, verify scope via existing `AccessTokenService.can_access_session` (or its agent-level equivalent for new sessions).
- `mcp_caller`: enforce `policy.require_caller_in_route` by calling existing `AppMCPRoutingService` checks; the service does not re-implement them.
- `system_trigger`: only accepted when `policy.allow_system_trigger_fastpath=True`. We then assert the invariant — `policy.expected_owner_id == agent.owner_id == sender.platform_user_id` — and return. All three values are pre-set by `SessionSender.from_system_trigger(owner_user_id=agent.owner_id)` and by the caller building the policy, so we are asserting a structural property rather than skipping the check. If the invariant fails, raise; the system_trigger path was constructed incorrectly.

**No new access primitives.** Every check delegates to an existing service or asserts a structural invariant. `assert_access` is a dispatcher to existing logic, not a re-implementation.

**Callers:** A2A (every dispatch method), App MCP (every send), web-UI session create, task execution, scheduler. ≥5 callers — rule satisfied.

### 4.4 Attachment handling — deferred until a real second caller exists

`normalize_attachments` is **not** in the initial service shape. Verification at planning time: A2A's `_extract_text_from_parts` is text-only — A2A does not upload `FilePart` content through any session-creation path today. App MCP carries text only. Internal triggers carry text only. Web-UI does carry `file_ids`, but it goes through the existing `POST /messages` path which already calls `SessionService.send_session_message` directly with `file_ids` (the `MessageService.prepare_user_message_with_files` flow). Adding the method now means N=1 production caller at merge time, which violates the §10 anti-goal directly.

**Decision: keep attachment handling in the per-channel code.** `ingest_inbound_message` exposes a `file_ids: list[UUID] | None` pass-through (already-uploaded `FileUpload.id`s) which is forwarded verbatim to `SessionService.send_session_message`. That covers any in-scope caller that ever needs it.

**When to revisit.** Add `normalize_attachments` to the service when a second channel actually needs cross-channel attachment normalization — likely email (auto-upload MIME parts) plus a future push channel (Telegram inline photo, Google Chat attachment). Both are out of scope here. The trip wire is real: do not add this method until two real callers exist.

### 4.5 Composition with existing services

`ChannelIngestionService` is a **client** of these, never their replacement:

- `SessionService.create_session` — sole DB-insert path.
- `SessionService.send_session_message` — sole "drop user message + kick stream" path.
- `SessionService.initiate_stream` — invoked transitively.
- `SessionService.ensure_environment_ready_for_streaming` — A2A still calls this directly to preserve its error-handling shape; the service does not call it for callers.
- `MessageService.create_user_message_and_emit_event` — used by callers who opt out of stream initiation (webhook today; not used here).
- `AccessTokenService.can_access_session` — invoked from `assert_access` for A2A.
- `AppMCPRoutingService` — invoked from `assert_access` for App MCP.

The service is **glue**, not a replacement.

---

## 5. Per-Channel Migration Analysis

For each channel: a BEFORE block (current code, abbreviated) and an AFTER block (the equivalent call to `ChannelIngestionService`). The line delta should net negative per channel — if it doesn't, we got the abstraction wrong.

### 5.1 A2A

**Files affected:**
- `backend/app/services/a2a/a2a_request_handler.py` — `handle_message_send`, `handle_message_stream`, `_stamp_new_session`, `_session_access_token_id`, `_integration_type_for_new_session`.

**BEFORE (`handle_message_send`, `:224-340` — 137 lines including the install-gate block):**

```
session_id = self._parse_session_scope(task_id)
is_new_session = session_id is None
if session_id is not None:
    await SessionService.ensure_environment_ready_for_streaming(...)
gate_result = InstallGateDispatcher.check(db, self.agent)
if gate_result:
    ... synthetic Task ...
result = await SessionService.send_session_message(
    session_id=session_id,
    user_id=self.user_id,
    content=content,
    file_ids=None,
    agent_id=self.agent.id if is_new_session else None,
    access_token_id=self._session_access_token_id(),
    integration_type=self._integration_type_for_new_session() if is_new_session else None,
    backend_base_url=self.backend_base_url,
    get_fresh_db_session=self.get_db_session,
)
```

**AFTER:**

```
sender = SessionSender.from_a2a(self.a2a_token_payload, self.access_token_id, self.user_id)
thread_key = self._parse_session_scope(task_id)   # unchanged — still A2A-internal scope-aware
if thread_key is not None:
    await SessionService.ensure_environment_ready_for_streaming(...)
gate_result = InstallGateDispatcher.check(db, self.agent)
if gate_result:
    ... synthetic Task ...
result = await ChannelIngestionService.ingest_inbound_message(
    db=db_for_call,
    agent=self.agent,
    sender=sender,
    thread_key=thread_key,
    content=content,
    integration_type=self._integration_type_for_new_session() or "a2a",
    access_policy=ChannelAccessPolicy(
        expected_owner_id=self.agent.owner_id,
        require_access_token_scope=self.a2a_token_payload,
    ),
    get_fresh_db_session=self.get_db_session,
    backend_base_url=self.backend_base_url,
)
```

**Goes away:** `_stamp_new_session` becomes a no-op for the core handler; stamping moves into `resolve_or_create_session`. The `if is_new_session else None` conditionals on `integration_type` / `agent_id` disappear (the service decides from `thread_key`).

**Stays:** install-gate synthetic `Task` response (A2A-specific), SSE streaming and `A2AEventMapper`, `_parse_session_scope` (A2A-only — fails the ≥2-callers rule so it doesn't move).

**Line delta (measured).** A2A's `handle_message_send` region (`:224-340`, 137 lines) loses the `is_new_session` branching (~12 lines), the duplicated `_session_access_token_id() / _integration_type_for_new_session()` plumbing as separate arguments (~6 lines), and the post-create stamping in `_stamp_new_session` (varies by subclass, ~5-10 lines in the override). Net handler delta: ~25-30 lines removed; ~10 lines added for the `SessionSender` + policy construction. Service gains the consolidated logic once.

### 5.2 App MCP

**Files affected:**
- `backend/app/services/app_mcp/app_mcp_request_handler.py` — `_resolve_session`, `_create_identity_session`, `_handle_inner`.

**BEFORE (`_resolve_session` create branches `:220-308`, 89 lines including resume + regular + identity; `_handle_inner` message injection `:90-145`, ~56 lines):**

```
session_data = SessionCreate(agent_id=routing_result.agent_id, mode=routing_result.session_mode)
session = SessionService.create_session(
    db_session=db, user_id=agent.owner_id, data=session_data, integration_type="app_mcp",
)
session.caller_id = user_id
session.session_metadata = {**(session.session_metadata or {}), "app_mcp_route_type": ..., ...}
db.add(session); db.flush(); db.refresh(session)
```

**AFTER:**

```
sender = SessionSender.from_app_mcp(caller_user_id=user_id)
ChannelIngestionService.assert_access(
    agent=agent, sender=sender,
    policy=ChannelAccessPolicy(
        expected_owner_id=agent.owner_id,
        require_caller_in_route=True,
    ),
)
session, created = ChannelIngestionService.resolve_or_create_session(
    db=db,
    agent=agent,
    sender=sender,
    thread_key=None,
    integration_type="app_mcp",
    extra_session_kwargs={
        "session_metadata_extra": {
            "app_mcp_route_type": routing_result.route_source,
            "app_mcp_route_id": str(routing_result.route_id),
            "app_mcp_agent_name": routing_result.agent_name,
            "app_mcp_session_mode": routing_result.session_mode,
            "app_mcp_match_method": routing_result.match_method,
        },
    },
)
```

`_resolve_session` reduces to: resume-branch (calls `resolve_or_create_session` with `thread_key=existing_session_id`); routing call; create-branch (calls `resolve_or_create_session` with `thread_key=None`). Identity routing differs only in the `sender` shape and the owner-resolution arg inside the service.

**AI routing stays in App MCP.** Router runs before the service; the service is called with an already-selected `Agent` (see §7.4).

**Message injection (`:103-108`):** `MessageService.create_message(role="user", ...)` + `stream_and_collect_response(...)` becomes a single `ingest_inbound_message(thread_key=session.id, ...)` call. The MCP-specific outbound collector stays in the MCP module — outbound is not unified.

**Line delta (measured).** Of the 89 lines in `_resolve_session`'s create branches (regular + identity) plus the ~56 lines in `_handle_inner`'s message-injection region, the migration consolidates the session-creation stamping (~25 lines), the regular/identity create-only divergence (~15 lines), and the `MessageService.create_message` + `stream_and_collect_response` plumbing (~10 lines) into service calls. Net handler delta: ~50 lines removed; ~15 lines added for `SessionSender`/policy construction and the `ingest_inbound_message` call. The MCP-specific outbound collector stays.

### 5.3 Web-UI session creation

**File:** `backend/app/api/routes/sessions.py:158`.

**The structural wrinkle.** Web-UI flow: (1) `POST /sessions` creates an empty `Session` — *no message yet*; (2) `POST /messages` calls `SessionService.send_session_message`. The other three channels create-and-send in one call.

**Decision: migrate only `POST /sessions`** through `resolve_or_create_session` (not `ingest_inbound_message`). `POST /messages` already goes through `SessionService.send_session_message`; do not double-wrap.

This is the load-bearing design call: `resolve_or_create_session` is a public method specifically so the UI can use it without faking a message.

**BEFORE (`backend/app/api/routes/sessions.py:152-166`, 15 lines including the active-environment guard):**

```
new_session = SessionService.create_session(
    db_session=session, user_id=user_id, data=session_in,
    guest_share_id=guest_share_id,
    dashboard_block_id=session_in.dashboard_block_id,
)
```

**AFTER:**

```
sender = SessionSender.from_webui(current_user)
new_session, _ = ChannelIngestionService.resolve_or_create_session(
    db=session,
    agent=agent,
    sender=sender,
    thread_key=None,
    integration_type=None,                   # web-UI sessions intentionally untagged
    extra_session_kwargs={
        "guest_share_id": guest_share_id,
        "dashboard_block_id": session_in.dashboard_block_id,
        "mode": session_in.mode,
    },
)
```

Ownership/permission checks at `:135-150` stay in the route — the guest-share-vs-authenticated branching is intertwined and doesn't belong in the service.

**Line delta (measured).** Route loses ~5 lines and gains ~10. Net **slightly positive** (~5 lines added) because the route picks up `SessionSender.from_webui` + policy boilerplate. The value here is *not* line reduction — it's that `Session` rows created via the web-UI go through the same stamping path as the other channels, removing a class of drift bugs. See §7.1 for the accepted trade-off framing.

### 5.4 Internal triggers — three distinct shapes

The reviewer's audit found three different shapes inside what we were calling "internal triggers". The migration treats them as three distinct cases:

| Site | Sender kind | Service method | Why |
|------|-------------|----------------|-----|
| `agent_schedule_scheduler._execute_static_prompt` (`:141`) | `system_trigger` | `ingest_inbound_message` | Cron-fired, no human caller, creates session + sends message. |
| `agent_schedule_scheduler._execute_script_trigger` (`:361`) | `system_trigger` | `ingest_inbound_message` | Cron-fired (only the non-OK branch fires a session), same shape as static_prompt. |
| `input_task_service.execute_task` (`:803`) | `task_executor` | `ingest_inbound_message` | Human-initiated from the route layer, carries a real `user_id`, creates session + sends message. |
| `input_task_service.start_session_for_task` (`:1271`) | `task_executor` | `resolve_or_create_session` **only** | Human-initiated, creates session and links it to the task — but does NOT send a message. The route returns; the user types in chat later. |

**BEFORE (scheduler static_prompt, `agent_schedule_scheduler.py:141-148`, 8 lines plus ~60 lines of surrounding logging/error handling):**

```
result = await session_service.send_session_message(
    session_id=None,
    agent_id=agent.id,
    user_id=agent.owner_id,
    content=message,
    initiate_streaming=True,
    get_fresh_db_session=lambda: DBSession(engine),
)
```

**AFTER:**

```
sender = SessionSender.from_system_trigger(owner_user_id=agent.owner_id)
result = await ChannelIngestionService.ingest_inbound_message(
    db=db_session,
    agent=agent,
    sender=sender,
    thread_key=None,
    content=message,
    integration_type="schedule",
    access_policy=ChannelAccessPolicy(
        expected_owner_id=agent.owner_id,
        allow_system_trigger_fastpath=True,
    ),
    get_fresh_db_session=lambda: DBSession(engine),
)
```

**BEFORE (`input_task_service.execute_task`, `:790-846`, 57 lines):** creates session with `SessionService.create_session(user_id=user_id, source_task_id=task.id)` then immediately calls `SessionService.send_session_message(session_id=new_session.id, user_id=user_id, content=content, file_ids=file_ids, ...)`.

**AFTER:**

```
sender = SessionSender.from_task_execution(task=task, executing_user_id=user_id)
result = await ChannelIngestionService.ingest_inbound_message(
    db=db_session, agent=agent, sender=sender,
    thread_key=None,
    content=content,
    file_ids=file_ids,
    integration_type="task",
    access_policy=ChannelAccessPolicy(
        expected_owner_id=agent.owner_id,
        # task_executor: assert sender.platform_user_id == user_id (a real check, not a fast-path)
    ),
    get_fresh_db_session=create_session,
)
# Then: InputTaskService.link_session(...) as today.
```

**BEFORE (`input_task_service.start_session_for_task`, `:1260-1287`, 28 lines):** creates session via `SessionService.create_session(user_id=user_id, source_task_id=task.id)` — **no message is sent**, the route returns and the user types in chat later.

**AFTER:**

```
sender = SessionSender.from_task_execution(task=task, executing_user_id=user_id)
session, _ = ChannelIngestionService.resolve_or_create_session(
    db=db_session, agent=agent, sender=sender,
    thread_key=None,
    integration_type="task",
    extra_session_kwargs={"source_task_id": task.id, "title": task.current_description[:100], "mode": mode},
)
# Then: InputTaskService.link_session(...) as today.
```

**Why `:1271` is `resolve_or_create_session`-only.** It is structurally the same case as `POST /sessions` (§5.3): create empty session, route returns, user types later. Trying to force it through `ingest_inbound_message` would require synthesizing a fake first message, which is exactly what `resolve_or_create_session` exists to avoid.

**Benefits dropping out for free (scheduler + task-exec):** `integration_type="schedule"`/`"task"` is consistently stamped (today: `None`); `session_metadata["schedule_id"]`/`["task_id"]` is stamped consistently (today: nothing); `task_executor` retains the real owner check; `system_trigger`'s trust is asserted via invariant rather than skipped.

**Line delta (measured).** Scheduler static_prompt: ~8 lines BEFORE → ~12 lines AFTER (slightly positive, but `_execute_script_trigger` follows the same pattern, so consolidating both yields neutral). `execute_task` (`:803`): 57-line region → loses ~10 lines of session-create + link-then-send sequencing, gains ~12 lines of constructor + policy. **Net neutral.** `start_session_for_task` (`:1271`): 28-line region → loses ~6 lines, gains ~10 lines. **Slightly positive.** Honest framing: internal triggers are a *parity* migration, not a line-reduction one. See §1.3 for the framing.

**Parity:** at end of Phase 5, every internal-trigger session has the same metadata shape and access-control narrative as A2A and App MCP sessions.

---

## 6. How to Add a Future Channel (the forward-looking deliverable)

This is the recipe for adding any future inbound channel — Telegram, Google Chat, Slack, anything. It is intentionally one paragraph and one code block.

**Recipe.** Write a transport parser that:
1. Authenticates the request and produces a `SessionSender` (use an existing constructor, or define an inline `SessionSender(kind="...", external_id="...", display_name=..., platform_user_id=...)`).
2. Maps the provider's thread identifier (chat_id, thread_name, etc.) to a platform `Session.id` if you can, else `None`. This is your `thread_key`. Storage of provider↔session mapping is the new channel's own responsibility — there is no `ChannelThread` table.
3. Extracts `content: str`. If the channel has attachments, the parser uploads them via existing `FileService` paths and passes the resulting `file_ids: list[UUID]` to the service. Cross-channel attachment normalization is **not** part of this service today — see §4.4.
4. Constructs a `ChannelAccessPolicy` expressing what gates apply.
5. Calls `ChannelIngestionService.ingest_inbound_message(...)`.

That is the whole platform-side contract. No ABC to implement, no registry to register with, no metadata schema to declare, no migration to author. The new channel module is a transport parser plus its own outbound delivery; everything in between is the service.

**Example (sketch only — not part of this plan):**

```python
# services/telegram/telegram_inbound.py (hypothetical, future)
async def handle_telegram_update(update: TelegramUpdate, db: DBSession) -> None:
    sender = SessionSender(
        kind="external_attested",   # would need to be added to Literal at that time
        external_id=str(update.message.from_.id),
        display_name=update.message.from_.username,
        platform_user_id=None,   # external_attested identities don't have one until linked
    )
    thread_key = telegram_thread_storage.lookup(update.message.chat.id)  # channel-owned mapping
    content = update.message.text or ""
    # If the channel has photos/documents, upload via FileService first and pass file_ids.
    await ChannelIngestionService.ingest_inbound_message(
        db=db, agent=agent, sender=sender,
        thread_key=thread_key, content=content,
        integration_type="telegram",
        access_policy=ChannelAccessPolicy(...),
        get_fresh_db_session=...,
    )
```

If the new channel needs cross-channel features (auto-create platform users from external attestation, normalized inbound audit, etc.), **that is a separate plan**, not an extension of this one.

---

## 7. Risks and Open Questions

### 7.1 Web-UI structural mismatch (acknowledged)

Web-UI creates an empty session, then sends messages separately. `resolve_or_create_session` is public precisely so the UI can use it without faking a message. Trade-off: web-UI gets less line-savings than the other three channels. Accepted — uniform vocabulary matters more than per-channel line count.

### 7.2 `system_trigger` vs `task_executor` trust model (resolved)

**Resolved by splitting the sender kinds (§3.1, §4.3).** The reviewer's audit caught that `input_task_service.py:803` carries a real human `user_id` from the route layer, which `send_session_message` validates at `session_service.py:1250` via `chat_session.user_id != user_id`. Collapsing it into `system_trigger` with a blanket fast-path allow would have widened trust silently.

The current design:
- `system_trigger` (cron-fired only) accepts the request after asserting the invariant `policy.expected_owner_id == agent.owner_id == sender.platform_user_id`. The constructor `SessionSender.from_system_trigger(owner_user_id=agent.owner_id)` enforces the second equality; the caller building the policy enforces the first. If either fails, `assert_access` raises. We are asserting a structural property, not skipping the check.
- `task_executor` runs a **real** `policy.expected_owner_id == sender.platform_user_id` check — equivalent to the `chat_session.user_id != user_id` guard in today's code. Plus per-task ACLs continue to be checked by `input_task_service.verify_agent_access` before `ingest_inbound_message` is ever called.

Trust is now visible in code (kind + policy) instead of hidden inside `send_session_message`. Resolved.

### 7.3 `get_session_sender(session)` read-site audit

Migration changes no DB columns, but each existing read site of `Session.caller_id` and `Message.user_id` should be audited — none should rely on `caller_id` being NULL specifically for A2A or non-NULL specifically for App MCP. Grep `caller_id` under `backend/app/services/` and `backend/app/api/`, verify each match. Carry the audit in Phase 2.

### 7.4 App MCP AI routing layer (resolved)

`AppMCPRoutingService.route_message` runs **before** the service. The service receives an already-selected `Agent`. Explicit boundary.

### 7.5 "Service with no users" hazard

If only A2A lands and App MCP slips, we have infrastructure for one caller. **Sequencing rule:** Phase 3 (App MCP) merges within 2 weeks of Phase 2 (A2A). Otherwise revert Phase 2. The service must have ≥2 real users by the end of the same release cycle.

### 7.6 `integration_type` proliferation

Two new values (`"schedule"`, `"task"`) plus an explicit convention for web-UI (currently `None`). Audit consumers of `Session.integration_type` in backend and frontend before Phase 5; document the canonical set in a comment on the `Session` model.

### 7.7 `normalize_attachments` — deferred (resolved)

The reviewer verified A2A's `_extract_text_from_parts` is text-only — A2A does not upload `FilePart` content through any session-creation path. So `normalize_attachments` would ship with N=1 production caller (web-UI) and trip the §10 anti-goal at merge time. **Deferred.** The service exposes `file_ids: list[UUID] | None` on `ingest_inbound_message` as a pass-through; per-channel attachment handling stays where it is. Revisit when a real second caller exists (likely email + a future push channel). See §4.4.

### 7.8 Open: `assert_access` for resumed sessions?

Today A2A re-checks scope on every dispatch; App MCP re-checks ownership via `WHERE caller_id = user_id`. We preserve that — `ingest_inbound_message` calls `assert_access` regardless of `thread_key`.

---

## 8. Phase Plan

Each phase is **one PR**, independently mergeable. No cross-phase atomic bundles.

**Note on the ≥2-callers contract test during Phase 2.** After Phase 2, only A2A uses the service, so the §9.4 contract test would fail with N=1 per public method. We preserve per-PR mergeability (the property §8 promises) by shipping the test in a relaxed mode in Phase 2 and tightening it in Phase 3's PR. See the Phase 2 / Phase 3 DoD entries below.

### Phase 0 — `SessionSender` and supporting types

- Add `backend/app/models/sessions/session_sender.py` with `SessionSender`, `get_session_sender`, five constructors (`from_a2a`, `from_app_mcp`, `from_webui`, `from_task_execution`, `from_system_trigger`), `ChannelAccessPolicy`, `IngestionResult`.
- Re-export from `app/models/sessions/__init__.py` and `app/models/__init__.py`.
- Pure-Python tests verifying the reader against synthetic `Session` rows.

**DoD:** `from app.models import SessionSender, get_session_sender` works; tests pass; no other code touched.

### Phase 1 — `ChannelIngestionService` shell

- Add `backend/app/services/sessions/channel_ingestion_service.py` with three methods (`ingest_inbound_message`, `resolve_or_create_session`, `assert_access`), fully implemented against existing primitives.
- API-level scenario tests exercising each method through an admin-guarded debug route (or a temporary `/api/v1/_test/channel_ingestion` route removed at Phase 2).
- Add the §9.4 contract test asserting `caller_modules >= 1` for each public method. (Tightened to `>= 2` in Phase 3 — see §9.4.)

**DoD:** Service exists; tests prove `resolve_or_create_session` + `ingest_inbound_message` produce sessions/messages identical to a direct `SessionService.send_session_message` call. Contract test passes with N=1 threshold. No real channel uses it yet.

### Phase 2 — A2A migration

- Migrate `handle_message_send` and `handle_message_stream` through `ingest_inbound_message`.
- Reconcile `ExternalA2AContextHandler` overrides — fold into `ChannelAccessPolicy` / `extra_session_kwargs` or keep as targeted overrides.
- Delete duplicated branches: `_stamp_new_session` (core), the `is_new_session` conditionals, `_session_access_token_id` becomes a constructor arg.
- Tests: existing A2A scenarios in `backend/tests/api/` + new scope-violation, gate-blocked, resumed-session cases.
- **Contract test stays at `caller_modules >= 1`.** This is the documented bridge state — visible in the test source so any reviewer can see we have one production caller until Phase 3 lands.

**DoD:** A2A tests green; `a2a_request_handler.py` line count drops; `caller_id` audit (§7.3) done. Contract test still relaxed.

### Phase 3 — App MCP migration (the keep-alive)

- Migrate `_resolve_session` (regular + identity) and `_handle_inner` message injection.
- Routing (`AppMCPRoutingService`) untouched.
- Tests: `tests/api/app_mcp/` — `send_message`, identity `send_message`, resume by `context_id`, identity-binding revocation mid-session.
- **Tighten the contract test:** `caller_modules >= 1` → `caller_modules >= 2`. This change is part of the same PR as the App MCP migration so the test diff records the moment we cleared the bridge state.

**DoD:** App MCP tests green; service has **two** real users; contract test now at `>= 2`. §7.5 hazard cleared.

### Phase 4 — Web-UI session creation

- `POST /api/v1/sessions/` calls `resolve_or_create_session`.
- `POST /messages` untouched.
- Tests: existing session-creation API tests; add guest-share coverage.

**DoD:** Existing tests green; web-UI sessions show consistent `integration_type` convention and `session_metadata` shape.

### Phase 5 — Internal triggers

- Migrate `agent_schedule_scheduler._execute_static_prompt` and `_execute_script_trigger`.
- Migrate `input_task_service` paths at `:803` and `:1271`.
- Grep audit for any other internal-trigger session creation.
- Tests: schedule-fires-session, task-trigger-session scenarios.

**DoD:** All in-scope channels migrated; ≥2-callers rule (`caller_modules >= 2`) re-verified for every public method via the §9.4 contract test; the three-method service shape is intact (no attachment-normalization sneak-in — see §4.4).

---

## 9. Test Strategy

### 9.1 Existing fixtures and conventions

Per `backend/tests/README.md`: API-only, scenario-based, savepoint-rolled-back. No direct DB access in tests. Tests live under `backend/tests/api/` organised by domain.

### 9.2 New fixtures

- `make_session_sender(kind, ...)` — builds a `SessionSender` of any kind (helper, not a fixture, so tests can compose).
- `make_access_policy(...)` — same idea for `ChannelAccessPolicy`.
- No new database fixtures needed — the service exercises existing tables only.

### 9.3 Test placement

- Unit-ish tests for `SessionSender` / `get_session_sender` / constructors: `backend/tests/models/sessions/session_sender_test.py` (pure-Python, no HTTP). One narrow exception to the "API-only" rule for the value-type module, justified because there is no API surface to exercise.
- Service tests: `backend/tests/api/sessions/channel_ingestion_test.py` — uses a debug route (Phase 1) or, post-Phase-1, exercises the service transitively through A2A / App MCP / web-UI tests.
- Channel migration tests: extend the existing scenario tests under `backend/tests/api/a2a/`, `backend/tests/api/app_mcp/`, `backend/tests/api/sessions/`, `backend/tests/api/agents/` (schedules and tasks).

### 9.4 Contract test for the ≥2-callers rule

A meta-test at `backend/tests/architecture/channel_ingestion_callers_test.py` enforces the rule. Specification (not vibes):

- **Search root:** `backend/app/` only. Never the repo root, never `frontend/`, never `backend/tests/`, never `backend/alembic/`.
- **File excludes:** the definition site `backend/app/services/sessions/channel_ingestion_service.py`, anything matching `**/*_test.py`, anything under `backend/tests/**`.
- **What to count:** for each public method on `ChannelIngestionService` (`ingest_inbound_message`, `resolve_or_create_session`, `assert_access`), grep for `ChannelIngestionService.<method>(` and `from app.services...channel_ingestion_service import ...` then for direct method invocations. Collect the set of **module file paths** that contain at least one match. **Count distinct modules**, not call sites — two callers inside the same file count as one module.
- **Threshold:** initially `caller_modules >= 1` (Phase 1 ships the test in this relaxed state because only the debug route uses the service; Phase 2 keeps it at `>= 1` as the documented bridge — see §8). **Phase 3's PR tightens the threshold to `caller_modules >= 2`** in the same diff that adds App MCP as the second module, so the test source records the moment we cleared the bridge state.
- **Steady-state assertion (after Phase 3):** each public method has `caller_modules >= 2`. The structural guardrail against the "service grew a one-caller method" failure mode.

Runs as part of normal CI. Exception list (if any) lives next to the test with a comment explaining why each exception exists.

### 9.5 Validating consolidation

At Phase 5 close, record the measured deltas per channel and confirm the per-channel-shaped expectations from §5 hold:

- A2A (`a2a_request_handler.py`): **net negative** (~25-30 lines removed, ~10 added).
- App MCP (`app_mcp_request_handler.py`): **net negative** (~50 lines removed, ~15 added).
- Web-UI (`api/routes/sessions.py:158`): **net neutral / slightly positive** (~5 added).
- Scheduler (`agent_schedule_scheduler.py`): **net neutral**.
- Task execution (`input_task_service.py:803`): **net neutral**.
- Task create-only (`input_task_service.py:1271`): **slightly positive**.

The validation is *not* "deleted lines > added lines overall" — that framing was wrong (see §1.3). The validation is that A2A and App MCP — the two production send paths — show real consolidation, and the others land on uniform stamping at acceptable cost. If A2A or App MCP comes out neutral or positive, the abstraction was wrong; re-evaluate.

---

## 10. Anti-Goal Checklist

If during implementation any of the following appear, **stop and reconsider**:

- A `ChannelAdapter` abstract base class.
- A `ChannelType` enum or registry of adapters.
- A new `backend/app/models/channels/` or `backend/app/services/channels/` directory.
- A new database table whose name starts with `channel_`.
- A method on `ChannelIngestionService` with one caller.
- A `process_inbound(channel, raw)` signature where `raw` is a per-channel `Any`.
- An attempt to also unify outbound delivery, polling, or attachment storage.
- A migration file accompanying any PR in this plan.
- An OpenAPI client regeneration step in any phase's "definition of done". (None of the four migrations touches the API contract.)
- A `SessionSender.kind` value list that grows to more than 7 entries in this plan. (We have 6 active kinds + one reserved `anonymous` slot — see §3.1. The split between `task_executor` and `system_trigger` is a *correctness* requirement per the reviewer's audit, not a kind-list inflation; do not collapse them back.)
- An `IngestionResult` field that holds channel-specific data the caller has to type-check.
- A `ChannelIngestionService` method that knows the name of any concrete channel (e.g., a `if integration_type == "a2a": ...` branch). The service must remain channel-agnostic; per-channel behaviour lives in the caller.

---

## 11. Project Conventions and File Placement

**Module placement (defended):**

- `backend/app/models/sessions/session_sender.py` — `SessionSender` is derived from existing `Session` columns; it belongs next to the session model. A `models/channels/` directory would imply a `Channel` table that does not exist.
- `backend/app/services/sessions/channel_ingestion_service.py` — the service is a thin orchestration over `SessionService`. As a peer of `SessionService` and `MessageService` it reflects what it does: session-shaped work. `services/channels/` is framework-shaped naming and is disallowed (§10).
- Re-exports in `backend/app/models/__init__.py` and `backend/app/services/sessions/__init__.py`.

**Patterns followed:**
- No `table=True` models added.
- All service methods take `db: DBSession` explicitly (`@staticmethod`, mirroring `SessionService`).
- `assert_access` raises (pick `PermissionError` or `ValueError` to match A2A's `_wrap_env_error` — decide in Phase 1). `IngestionResult.action == "error"` for soft failures, matching `send_session_message`.
- Log-prefix decision deferred to Phase 1 (likely `[ChannelIngestion]` plus optional per-caller prefix passed in).
- Tests follow `backend/tests/README.md` (API-level, scenario-based). One narrow exception: `SessionSender` value-type unit tests.

**Related docs (read order, per `docs/README.md`):**
- `docs/application/agent_sessions/agent_sessions.md` — lifecycle, modes, integration types.
- `docs/application/a2a_integration/a2a_protocol/a2a_protocol.md`.
- `docs/application/app_mcp_server/app_mcp_server.md`.
- `docs/agents/agent_schedulers/agent_schedulers.md`.
- `docs/application/input_tasks/input_tasks.md`.
- `docs/development/backend/backend_development_llm.md`.

Email / webhook / webapp docs are deliberately not touched in this plan.

---

## 12. Summary Checklist

**Phase 0 — value type:**
- [ ] Create `backend/app/models/sessions/session_sender.py` (`SessionSender` with 7 kinds, `get_session_sender`, five constructors, `ChannelAccessPolicy`, `IngestionResult`); re-export.
- [ ] Pure-Python tests under `backend/tests/models/sessions/`.

**Phase 1 — service shell:**
- [ ] Create `backend/app/services/sessions/channel_ingestion_service.py` with **three methods** (`ingest_inbound_message`, `resolve_or_create_session`, `assert_access`); re-export.
- [ ] Admin-guarded debug route (or temporary `/api/v1/_test/...`) for end-to-end exercise.
- [ ] Scenario tests in `backend/tests/api/sessions/channel_ingestion_test.py`.
- [ ] Add §9.4 contract test at `caller_modules >= 1` threshold.

**Phase 2 — A2A:**
- [ ] Replace `send_session_message` call in `handle_message_send` / `handle_message_stream` with `ingest_inbound_message`.
- [ ] Reconcile `ExternalA2AContextHandler` overrides.
- [ ] Delete redundant `_stamp_new_session` branches.
- [ ] `caller_id` audit (§7.3) complete; A2A tests green.
- [ ] Contract test stays at `>= 1` (documented bridge state).

**Phase 3 — App MCP:**
- [ ] Migrate `_resolve_session` (regular + identity) and `_handle_inner` message injection.
- [ ] Tighten contract test to `caller_modules >= 2` in the same PR.
- [ ] Routing untouched; App MCP tests green. Service has ≥2 real users.

**Phase 4 — web-UI:**
- [ ] `POST /api/v1/sessions/` calls `resolve_or_create_session`; guest-share path migrated; `POST /messages` untouched; tests green.

**Phase 5 — internal triggers:**
- [ ] Migrate `_execute_static_prompt` and `_execute_script_trigger` (kind: `system_trigger`, method: `ingest_inbound_message`).
- [ ] Migrate `input_task_service.execute_task` at `:803` (kind: `task_executor`, method: `ingest_inbound_message`).
- [ ] Migrate `input_task_service.start_session_for_task` at `:1271` (kind: `task_executor`, method: **`resolve_or_create_session` only — does not send a message**).
- [ ] Grep audit confirms no other internal trigger bypasses the service.
- [ ] Schedule + task scenario tests green.

**Validation:**
- [ ] Contract test (§9.4) at `caller_modules >= 2` passes for every public method.
- [ ] Measured line deltas recorded per channel (A2A and App MCP: net negative; web-UI, scheduler, task-exec, `:1271`: net neutral or slightly positive — see §5).
- [ ] Anti-goal checklist (§10) re-read clean. No `normalize_attachments` snuck back in (see §4.4).

**Out of scope — confirm not touched:** no new tables/migrations; no frontend changes; no client regeneration; no `services/channels/` directory; email/webhook/webapp paths unchanged; outbound delivery unchanged.

---

*End of plan.*
