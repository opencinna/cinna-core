# Session-start Identity Injection via System Prompt — Implementation Plan

## Overview

Give every agent session a **prompt-channel** answer to "who am I talking to right
now?" by injecting a small, server-verified identity block into the system prompt
that the agent-env builds per message. The identity is chosen per session
integration type: for owner/web/initiator-bearing channels we inject the
**session initiator's** identity; for impersonation channels (A2A, direct MCP
connector) and system-run channels (scheduler, handover) we inject the **agent
owner's** identity, because direct sharing assumes the caller acts under the
owner's name.

This rides the **existing** per-message `session_context` payload already threaded
from `MessageService._build_session_context` → agent-env `/chat/stream` →
`PromptGenerator.build_session_context_section`. We add one resolver host-side, one
key on the `session_context` dict, and one additive rendering branch in env-core.

### Relationship to Feature 1 (credentials.json current-user context)

This is the **session-time complement** to Feature 1
(`docs/plans/credentials_json_current_user_context_plan.md`). The two features are
deliberately distinct channels and must compose, not collide:

| | Feature 1 (credentials.json) | Feature 2 (this plan) |
|---|---|---|
| **Channel** | File — `workspace/credentials/credentials.json` | Prompt — system-prompt block |
| **Scope** | Per **install** (rebuilt on credential sync) | Per **session / message** |
| **Whose identity** | The **install OWNER** (`agent.owner_id`), always | The **session initiator OR owner**, chosen per integration type |
| **JSON/label key** | `current_user` list entry inside credentials.json | `<session_initiator>` prompt block (see §D) |
| **Mutability** | Owner-editable free-text `custom_details` | Read-only, server-derived from the live session row |
| **Built where** | `prepare_credentials_for_environment()` | `MessageService._build_session_context()` |

For an **owner's own web session**, both name the owner — consistent. For
**A2A / direct-MCP impersonation**, both name the owner — consistent. For an
**App-MCP route** where a real external caller is known, Feature 1's
credentials.json still names the install OWNER (the script acts as the owner) while
Feature 2's prompt block names the **CALLER** (the agent knows who is asking). This
divergence is **by design** and is documented in §D so the two never imply they are
the same record. That is the whole reason for choosing a distinct prompt label.

---

## Architecture Overview

### Data flow (additive — bold = new)

```
Inbound message  ─► MessageService.stream_message_with_events  (per message)
                       └─ _resolve_stream_context
                            └─ _build_session_context(db, session_db, env, agent)
                                 ├─ existing keys (integration_type, sender_email, …)
                                 └─ **session_identity_resolver.build_identity_block(db, session_db, agent)**
                                      │  picks OWNER vs INITIATOR by integration_type
                                      │  loads User row(s) for full_name/email/username
                                      ▼
                            session_context["session_initiator"] = {...}   (new key)
                       └─ wrap into session_state (+ existing HMAC signature, unchanged)
                       └─ POST {base_url}/chat/stream
                                 ▼
   agent-env routes.chat_stream → _store_session_context → sdk_manager.send_message_stream
                                 ▼
   PromptGenerator.generate_{conversation,building}_mode_prompt(session_context)
        └─ build_session_context_section(session_context)
             └─ **renders <session_initiator> block when the key is present**   (REBUILD)
```

### Components touched

| Layer | File | Change |
|-------|------|--------|
| Resolver (new) | `backend/app/services/sessions/session_identity_resolver.py` | Pure-ish helper: pick owner vs initiator by integration type; load User row(s); build the identity dict |
| Host-side wiring | `backend/app/services/sessions/message_service.py` | Call the resolver inside `_build_session_context`; add `session_initiator` key |
| Env-core (REBUILD) | `backend/app/env-templates/app_core_base/core/server/prompt_generator.py` | Additive render branch in `build_session_context_section` |
| Model | — | **No model change, no migration** (all data already on `User` + `Session` + `MCPSessionMeta`) |
| Frontend | — | None |

### Migration: NONE

Confirmed: every field needed already exists.
- `Session.user_id`, `caller_id`, `identity_caller_id`, `sender_email`,
  `integration_type`, `access_token_id`, `guest_share_id`, `mcp_connector_id`,
  `agent_id`, `session_metadata` — all present
  (`backend/app/models/sessions/session.py`).
- `User.full_name`, `email`, `username`, `email_confirmed`
  (`backend/app/models/users/user.py` L37–44, L132).
- `MCPSessionMeta.authenticated_user_id` / `authenticated_user_email`
  (`backend/app/models/mcp/mcp_session_meta.py`) for the direct-MCP authenticated caller.
- `Agent.owner_id` (`backend/app/models/agents/agent.py` L111–113).

**No new persisted state. No Alembic revision.**

### Env REBUILD requirement: YES

`build_session_context_section` lives in
`backend/app/env-templates/app_core_base/core/server/prompt_generator.py`, which is
baked into the agent-env image. **Any edit here requires an environment rebuild**
for the new block to render. This is the single hard constraint of the feature. See
§B for a mitigation that keeps the env-core change minimal (and a fully host-side
fallback alternative if a rebuild must be avoided).

---

## §A — Taxonomy: which identity to inject per integration type

The platform already has the canonical sender taxonomy in
`backend/app/models/sessions/session_sender.py` (`get_session_sender(session)` maps
`Session.integration_type` → `SessionSenderKind`). We **reuse** it for the *kind*
classification but make the **owner-vs-initiator decision explicit** in the new
resolver, because the existing reader's `platform_user_id` is not always the
identity we want to *show* (e.g. for A2A it is already the owner; for direct `"mcp"`
it falls through to the owner; for `app_mcp` it is the caller).

> Rule of thumb: **IMPERSONATION channels → OWNER. KNOWN-CALLER channels →
> INITIATOR (the caller). NO-KNOWN-CALLER channels → OWNER or omit.**

| # | Integration type (`Session.integration_type`) | Sender kind | **Inject** | Identity source | Justification |
|---|---|---|---|---|---|
| 1 | `None` (web UI / owner's own session) | `webui_user` | **INITIATOR** | `session.user_id` → User | The session owner is the one talking; initiator == owner here. Requirement 3 (fair for owner's own sessions). |
| 2 | starts with `"a2a"` (core + external A2A) | `a2a_caller` | **OWNER** | `agent.owner_id` → User (== `session.user_id` for A2A) | IMPERSONATION. External caller is anonymous / token-scoped (`access_token_id`); the owner explicitly opened an A2A surface, so the agent acts under the owner's name. Requirement 2. |
| 3 | `"mcp"` (direct MCP **connector** session) | `platform_user` (reader fallback) | **OWNER** | `agent.owner_id` → User (== `connector.owner_id` == `session.user_id`) | IMPERSONATION. Direct connector sharing = "act as me." `session.user_id` is the connector owner by construction (`get_or_create_mcp_session`, `integration_type="mcp"`). The real OAuth caller, if ACL-granted, sits in `MCPSessionMeta` and is intentionally **not** surfaced as the prompt identity (impersonation contract). Requirement 2. |
| 4 | `"app_mcp"` (App MCP route) | `mcp_caller` | **INITIATOR** | `session.caller_id` → User | A real platform caller is known (`caller_id`), and App-MCP routing is a *routing* layer, not an impersonation grant — the agent should know who is actually asking. `session.user_id` is the agent owner; only the prompt names the caller. Composes with Feature 1 (creds.json still = owner). |
| 5 | `"identity_mcp"` (identity routing / external) | `mcp_caller` | **INITIATOR** | `session.identity_caller_id` → User | Same as App MCP: a real authenticated caller is known via `identity_caller_id`. Person-level routing addresses *a person asking on behalf of themselves*; surfacing the caller is the point. |
| 6 | `"email"` | `platform_user` (fallback) | **INITIATOR (no User row)** | `session.sender_email` (string) | The external email sender is a real human but has no platform `User`. Inject `email` only (and no name unless derivable). |
| 7 | guest share (`guest_share_id` set; `integration_type` is `None` for grant-based, kind `anonymous` for unauth) | `anonymous` / `webui_user` | **OWNER** (anon) / **INITIATOR** (grant) | anon → `agent.owner_id`; grant → `session.user_id` (the grant user) | Anonymous guests have **no known caller** → fall back to OWNER (the agent serves the owner's content; never claim a fake identity). Grant-based guests are authenticated users → `session.user_id` is that user. Detect via `session.guest_share_id is not None`. |
| 8 | `"task"` (human-initiated task execution) | `task_executor` | **INITIATOR** | `session.user_id` (the executing human) | `from_task_execution` stamps the real human's id as `session.user_id`. The agent should know which human kicked off the task. |
| 9 | `"schedule"` (cron / handover system trigger) | `system_trigger` | **OWNER** | `session.user_id` (== `agent.owner_id` by construction) | Runs as the owner by construction. No human caller; the owner is the right "on behalf of." Requirement: task_executor/system_trigger → owner. |
| 10 | any other / unknown | `platform_user` (fallback) | **OWNER** | `agent.owner_id` | Safe default: never invent a caller. If owner unresolvable → **omit the block** (§C). |

### Disambiguating direct-MCP (`"mcp"`) vs App-MCP (`"app_mcp"`)

These are genuinely different channels and the resolver branches on
`integration_type` first (never on `mcp_connector_id` alone):
- **`"mcp"`** = direct MCP connector session (`get_or_create_mcp_session`,
  `session.user_id = connector.owner_id`). `mcp_connector_id` is also set. → **OWNER**.
- **`"app_mcp"`** = universal App MCP router route. `caller_id` carries the real
  caller. → **INITIATOR (caller_id)**.
- **`"identity_mcp"`** = identity routing. `identity_caller_id` carries the caller.
  → **INITIATOR (identity_caller_id)**.

`get_session_sender` has **no `"mcp"` branch** (it falls through to
`platform_user` with `platform_user_id = session.user_id`). That is fine — the
resolver special-cases `"mcp"` explicitly and maps it to OWNER, so we never rely on
the reader's fallback for this decision.

---

## §B — Injection point

### Where the backend builds the block

New helper module `backend/app/services/sessions/session_identity_resolver.py`,
sitting alongside `session_sender.py` (which is a pure value type and must stay
DB-free per its own design constraints). The resolver **needs DB access** (it loads
the owner `User` and, for initiator channels, the caller `User` /
`MCPSessionMeta`), so it lives in the services layer, not the model layer.

```text
def build_identity_block(db, session, agent) -> dict | None
    # Returns the identity dict for the session_context, or None to omit.
    # 1. Classify: choose OWNER vs INITIATOR via the §A table (branch on
    #    session.integration_type, with guest_share_id pre-check).
    # 2. Load the chosen User row (or sender_email string for email).
    # 3. Build {"who": "owner"|"initiator", "full_name", "email", "username"}.
    # 4. Return None when nothing resolvable (graceful omit — §C).

def _resolve_owner_user(db, session, agent) -> User | None
def _resolve_initiator(db, session) -> dict | None   # User row OR {"email": sender_email}
```

`agent` is already loaded in `_resolve_stream_context` (passed into
`_build_session_context`), so the owner lookup is one `db.get(User, agent.owner_id)`.
When `agent` is `None` (agent_id is `SET NULL`-able), the resolver falls back to
`db.get(Agent, session.agent_id)` only if needed, else returns `None`.

### Where the block is added to `session_context`

Inside `_build_session_context(db, session_db, env, agent)` in
`backend/app/services/sessions/message_service.py` (currently builds the dict at
L422–431 and conditionally enriches through L549). Add **one** block after the base
dict, guarded so a resolver failure never breaks streaming:

```python
try:
    identity = session_identity_resolver.build_identity_block(db, session_db, agent)
    if identity:
        context["session_initiator"] = identity
except Exception:
    logger.exception("identity block build failed; omitting")  # never break the stream
```

(Import the resolver at module top; no import cycle — it only imports models +
`MCPSessionMeta`, which `message_service` already touches.)

### Per-message vs first-message-only — **decision: inject on EVERY message**

`_build_session_context` already runs **once per message** (confirmed:
`stream_message_with_events` → `_resolve_stream_context` → `_build_session_context`
rebuilds fresh per content batch — `message_service.py` ~L1690, L2415–2419). We
inject the block **every time**, for these reasons:
- It is **idempotent and small** (3–4 fields).
- It is **robust to SDK context resets / compaction** — the identity survives even
  if the SDK trims history. A first-message-only injection would silently vanish
  after a context reset.
- It requires **zero new "is-first-message" state** to track.
- The existing per-message context (Session ID, integration type) already follows
  exactly this every-message pattern, so this is consistent.

The wording "on session start / first message" in the requirement is satisfied
because the block is present from the very first message; injecting it on every
subsequent message is a strict superset and is simpler.

### Exact env-core rendering change

In `build_session_context_section(session_context)`
(`prompt_generator.py` L409–481), add an **additive** branch (after the existing
fields, before the trailing trust disclaimer). The block is rendered **only when
the key is present**, so older backends (payloads without `session_initiator`)
omit it — fully backward-compatible.

```python
initiator = session_context.get("session_initiator")
if initiator:
    who = initiator.get("who")               # "owner" | "initiator"
    name = initiator.get("full_name")
    email = initiator.get("email")
    username = initiator.get("username")
    lines.append("\n### Current Session Identity")
    if who == "owner":
        lines.append("- You are operating **on behalf of the agent owner** "
                     "(this connection runs under the owner's identity):")
    else:
        lines.append("- The current message was initiated by:")
    if name:
        lines.append(f"  - **Name**: {name}")
    if email:
        lines.append(f"  - **Email**: {email}")
    if username:
        lines.append(f"  - **Username**: {username}")
```

(Values are inert text appended to a Markdown list; no templating/eval. Keep the
existing `not integration_type and not backend_session_id` early-return — the
identity block participates in the same section that already gates on those, so a
truly empty context still returns `None`.)

### Host-side-rendered vs env-core-rendered — **recommendation: structured field + env-core render (accept the rebuild)**, with a documented host-side fallback

Two options:

- **(Chosen) Structured field rendered in env-core.** Backend ships the structured
  `session_initiator` dict; env-core formats it. Pros: the agent-env owns prompt
  formatting (consistent with how every other context field is rendered today);
  the wire payload stays data, not pre-baked prose; future tweaks to wording are an
  env-core concern. Con: needs a rebuild.
- **(Fallback) Host-side pre-formatted text.** Backend builds the Markdown string
  and ships it under a generic passthrough key that `build_session_context_section`
  already renders verbatim. This would require **no env-core change and no rebuild**
  — *if* such a passthrough key exists. Today it does **not**:
  `build_session_context_section` reads a fixed set of keys, so even the fallback
  needs a one-line env-core change to emit an arbitrary `session_initiator_text`
  string. Since both paths require an env-core touch, the structured option is
  preferred for cleanliness; the rebuild is unavoidable either way.

**Conclusion: a rebuild is required.** Bundle this change with any other pending
prompt_generator change to amortize the rebuild.

---

## §C — Where initiator identity comes from per channel + graceful fallback

| Channel | Initiator source | Fallback when unknown |
|---|---|---|
| web (`None`) | `db.get(User, session.user_id)` | omit |
| A2A (`a2a*`) | n/a — injects OWNER | omit if owner unresolvable |
| direct MCP (`"mcp"`) | n/a — injects OWNER | omit if owner unresolvable |
| App MCP (`"app_mcp"`) | `db.get(User, session.caller_id)`; if `caller_id` is None → omit (do **not** silently fall back to owner — keeps the channel honest) | omit |
| identity MCP (`"identity_mcp"`) | `db.get(User, session.identity_caller_id)`; None → omit | omit |
| email (`"email"`) | `session.sender_email` (string; **no User row**) → `{"who":"initiator","email": sender_email}` | omit if `sender_email` empty |
| guest (anon) | n/a — injects OWNER | omit if owner unresolvable |
| guest (grant) | `db.get(User, session.user_id)` | omit |
| task (`"task"`) | `db.get(User, session.user_id)` | omit |
| schedule (`"schedule"`) | n/a — injects OWNER (`session.user_id`) | omit |

**Fallback policy: OMIT-WHEN-UNKNOWN.** When the chosen User row is missing
(`SET NULL`'d FK, deleted user) or the email string is empty, `build_identity_block`
returns `None` and no block is rendered. We do **not** emit a synthetic "unknown"
identity — a missing block is unambiguous and avoids the agent claiming a fake
identity. Name fields default to whatever the User row has; `full_name` may be
`None` (rendered as just email+username), mirroring the
`current_user.full_name or current_user.email` convention used elsewhere.

---

## §D — Privacy / composition with Feature 1

### Field-name / label choice — **`<session_initiator>` (rendered as a "Current Session Identity" section)**

- **Payload key:** `session_context["session_initiator"]` (dict).
- **Rendered section heading:** `### Current Session Identity`.

Justification: Feature 1 uses the JSON id `current_user` inside credentials.json.
Feature 2 deliberately uses a **different label** so the two are never conflated:
- `current_user` (Feature 1) = the install **owner**, a file-channel record, stable
  per install.
- `session_initiator` (Feature 2) = **who is talking in this session**, which may
  be the owner *or* a different caller, recomputed per message.

A distinct label prevents a script author from assuming
`creds["current_user"].email == <the person in the prompt>` — which is **false** on
App-MCP/identity channels by design (Feature 1 = owner, Feature 2 = caller). The
prompt block explicitly states whether it represents the **owner** ("operating on
behalf of the agent owner") or an **initiator** ("the message was initiated by"),
so the model understands the relationship.

### Composition matrix

| Channel | Feature 1 (creds.json `current_user`) | Feature 2 (prompt `session_initiator`) | Consistent? |
|---|---|---|---|
| web / owner | owner | owner (initiator==owner) | ✅ same |
| A2A | owner | owner | ✅ same |
| direct MCP | owner | owner | ✅ same |
| App MCP route | owner | **caller** | ⚠️ **by design** (script acts as owner; prompt knows the asker) |
| identity MCP | owner | **caller** | ⚠️ **by design** |
| email | owner | external sender email | ⚠️ **by design** (no platform user) |
| schedule / handover | owner | owner | ✅ same |
| guest (anon) | owner | owner | ✅ same |

The ⚠️ rows are intentional and documented so the features compose. No secret data
crosses either channel — both carry only public identity (name/email/username).

---

## Phased Implementation

### Phase 1 — Identity resolver + per-type mapping (backend, no wiring yet)
- **New:** `backend/app/services/sessions/session_identity_resolver.py`
  - `build_identity_block(db, session, agent) -> dict | None`
  - `_resolve_owner_user(db, session, agent) -> User | None` (uses `agent.owner_id`,
    falls back to `db.get(Agent, session.agent_id)`).
  - `_resolve_initiator(db, session) -> dict | None` (web/task/grant → `user_id`;
    app_mcp → `caller_id`; identity_mcp → `identity_caller_id`; email →
    `sender_email`).
  - Classification branches exactly per the §A table; **guest pre-check**
    (`session.guest_share_id is not None`) before the `integration_type` switch.
  - Returns `{"who": "owner"|"initiator", "full_name", "email", "username"}` (email
    may be the only populated field for the email channel).
- Pure module-level functions; only imports `User`, `Agent`, `MCPSessionMeta`
  models. No new exports needed in `models/__init__.py` (it's a service).

### Phase 2 — Thread the block into `session_context`
- **Edit:** `backend/app/services/sessions/message_service.py`,
  `_build_session_context` (~L422–431): import the resolver at module top; after the
  base `context` dict is built, add the guarded `session_initiator` assignment
  (try/except, log-and-omit). No change to the HMAC signing — the new key is signed
  automatically because `sign_session_context` serializes the whole dict.
- No change to `_resolve_stream_context`, `send_message_to_environment_stream`,
  `agent_env_connector.stream_chat`, or the env-core `/chat/stream` route — the new
  key rides the existing `session_state.session_context` payload verbatim.

### Phase 3 — Env-core rendering + REBUILD
- **Edit:** `backend/app/env-templates/app_core_base/core/server/prompt_generator.py`,
  `build_session_context_section` (L409–481): add the additive
  `session_initiator` branch described in §B (covers both
  `generate_conversation_mode_prompt` L633–727 and
  `generate_building_mode_prompt` L506–631, since both call
  `build_session_context_section`).
- **REBUILD the agent-env image** so the new renderer ships. Until rebuilt, older
  containers simply ignore the new `session_initiator` key (forward-compatible —
  they never read it).

### Phase 4 — Tests (per `backend/tests/README.md`)
See the Test Plan below.

---

## Test Plan (per `backend/tests/README.md` — API-only, scenario-based)

> Read `backend/tests/README.md` and any `tests/api/sessions/README.md` (and
> `tests/api/agents/README.md`) before writing. Tests drive the API only; build
> state through endpoints/fixtures; assert via the seam the suite already uses to
> inspect the `session_context` / payload sent to the agent-env (the same stub used
> by existing streaming/session-context tests).

**Per-integration-type identity selection** — for each channel, create a session of
that type (via its endpoint/fixture), send a message, and assert the
`session_initiator` that reaches the agent-env `session_context`:
- **web (`None`)** → `who == "initiator"`, email == the session owner's email.
- **A2A (`a2a*`)** → `who == "owner"`, email == agent owner's email (even with a
  scoped `access_token_id`).
- **direct MCP (`"mcp"`)** → `who == "owner"`, email == connector/agent owner's
  email, even when a different OAuth caller exists in `MCPSessionMeta` (assert the
  caller's email does **not** appear).
- **App MCP (`"app_mcp"`)** → `who == "initiator"`, email == `caller_id` user's
  email (≠ owner).
- **identity MCP (`"identity_mcp"`)** → `who == "initiator"`, email ==
  `identity_caller_id` user's email.
- **email (`"email"`)** → `who == "initiator"`, `email == session.sender_email`,
  no `full_name`/`username`.
- **guest (anon)** → `who == "owner"`.
- **guest (grant)** → `who == "initiator"`, the grant user's email.
- **schedule / handover (`"schedule"`)** → `who == "owner"`.
- **task (`"task"`)** → `who == "initiator"`, the executing human's email.

**Graceful omit-when-unknown:**
- `app_mcp` session with `caller_id = None` → no `session_initiator` key in
  `session_context` (omitted, not synthetic-unknown).
- Owner User row missing / agent_id `SET NULL` for an owner-channel → key omitted;
  the rest of `session_context` (Session ID, integration type) still present.
- Resolver raising mid-build → stream still proceeds; assert the message still
  streams (try/except swallow) and the key is simply absent.

**Block reaches the payload / composition:**
- Assert the `session_initiator` dict is present in the signed `session_context`
  that the agent-env receives (HMAC still verifies — the new key is covered by the
  existing signer).
- Composition: for an `app_mcp` session, assert `session_initiator.email` ==
  caller while (if Feature 1 is also present) `current_user.email` == owner —
  documenting the by-design divergence.

**Env-core render (unit, optional):** a direct unit test of
`build_session_context_section` with a `session_initiator` dict present/absent,
asserting the "Current Session Identity" lines appear only when present and that an
empty context still returns `None` (backward-compat).

---

## Open Decisions (recommended choices in **bold**)

1. **App-MCP / identity-MCP → inject the CALLER (initiator), not the owner.**
   The requirement frames *direct* sharing as impersonation→owner, but App-MCP and
   identity-MCP are routing layers that carry a real `caller_id` /
   `identity_caller_id`. **Recommendation: inject the caller.** If you instead want
   *all* MCP-family channels to impersonate the owner, flip rows 4–5 to OWNER (the
   resolver makes this a one-line change per row). Flagging for your call.
2. **Direct-MCP authenticated caller is intentionally hidden.** For `"mcp"`
   sessions we inject the OWNER and ignore the `MCPSessionMeta` authenticated user.
   **Recommendation: keep hidden** (impersonation contract). Alternative: surface
   `mcp_user_email` as the initiator if present. Flagging.
3. **Inject on every message (chosen) vs first-message-only.**
   **Recommendation: every message** (idempotent, survives context resets, zero new
   state). Confirm you're OK with the tiny per-message cost (one extra `db.get`).
4. **Accept the env rebuild (chosen).** Both the structured and host-side-text
   options require an env-core edit, so a rebuild is unavoidable. **Recommendation:
   bundle with the next prompt_generator change.** Confirm rebuild is acceptable now.
5. **Label `session_initiator` / "Current Session Identity" (chosen)** vs
   `current_user_identity`. **Recommendation: `session_initiator`** — maximally
   distinct from Feature 1's `current_user`.
6. **Guest-anon → owner vs explicit "anonymous guest" note.**
   **Recommendation: owner** (never invent a caller). Alternative: render a literal
   "anonymous guest, no identity" line. Flagging.

---

## Summary Checklist

**Backend**
- [ ] New `session_identity_resolver.py`: `build_identity_block`,
      `_resolve_owner_user`, `_resolve_initiator` (per-§A classification, omit-when-unknown).
- [ ] `message_service._build_session_context`: guarded `session_initiator`
      assignment (try/except log-and-omit).
- [ ] No model change, **no migration**.

**Env-core (REBUILD)**
- [ ] `prompt_generator.build_session_context_section`: additive
      `session_initiator` render branch (backward-compatible).
- [ ] Rebuild the agent-env image.

**Testing**
- [ ] Per-integration-type identity selection (web/A2A/mcp/app_mcp/identity_mcp/
      email/guest-anon/guest-grant/task/schedule).
- [ ] Omit-when-unknown fallback (null caller, missing owner, resolver raises).
- [ ] Block reaches the signed `session_context`; HMAC still verifies.
- [ ] Composition with Feature 1 (app_mcp: prompt=caller, creds=owner).
