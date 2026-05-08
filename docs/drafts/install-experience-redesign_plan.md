# Install Experience Redesign — Implementation Plan

Three intertwined pillars:

1. **Single-screen install page** replacing the 4-step `InstallWizard`.
2. **Bundle-side credential provisioning** — publishers can ship AI credentials and/or service credentials with the bundle (publisher pays / publisher reuses), or mark service credentials as user-provided (default).
3. **Lazy / just-in-time credential resolution** — install never blocks on incomplete user-provided credentials; a pre-LLM gate at every channel boundary detects placeholders and emits a synthesised "setup needed" reply with a one-link setup URL.

Phasing is explicit so the wizard redesign and the runtime gate ship independently.

---

## 0. Glossary (plan-local)

- **Spec**: one entry of `AgentBundleRevision.required_credential_specs`.
- **Provided-by-publisher (PBP)**: a spec backed by the publisher's own `Credential` / `AICredential` row; foreign installs reference that row directly via the existing share mechanism.
- **Provided-by-user (PBU)**: a spec the publisher declares as belonging to each installer (e.g. each user's mailbox); install creates a placeholder `Credential` row and the runtime gate later prompts for fill-in.
- **Auto-prefill**: a lightweight server-side detection that returns a suggested existing user `Credential` for a spec (matched by type and name) so the install screen can pre-pick it.
- **Pre-LLM gate**: synchronous check executed on the request path of every user→agent dispatch (chat, MCP, A2A, webhook). Returns either "ok, proceed to LLM" or "blocked with setup URL" — never invokes the LLM in the latter case.
- **Setup URL**: an authenticated link to a focused credential-setup page rendering only the install's incomplete user-provided credentials.

---

## 1. Architecture Overview

```
                      ┌──────────────────────────────────┐
                      │ Publisher's bundle (revision)    │
                      │ required_credential_specs[]:     │
                      │   provided_by: publisher | user  │
                      │   publisher_credential_id        │
                      │   publisher_ai_credential_id     │
                      │   ...                            │
                      └──────────────┬───────────────────┘
                                     │ install
                                     ▼
   InstallService._setup_install_credentials  ──── PBP: link foreign row via
        │                                          AgentCredentialLink (Cred)
        │                                          OR AICredentialShare (AICred)
        │                                          referencing publisher row
        │
        └─ PBU: create placeholder Credential (is_placeholder=True), link it
           via AgentCredentialLink. No blocking — install activates.

   ── runtime, every channel ────────────────────────────────────────────────
   user message (chat / MCP / A2A / webhook)
        ▼
   InstallReadinessGate.check(install)
        │  iterates AgentCredentialLink rows + AI credential resolution
        │   PBU rows where is_placeholder=True → record "incomplete"
        │   PBP rows where publisher row missing/revoked → record "broken"
        ▼
   ok? → forward to existing message dispatch (LLM)
   not ok? → synthesise system message:
              "Setup needed: <url>"
              + structured payload (status, missing[], setup_url)
              channel-specific renderer turns it into a user-visible reply.

   ── setup page ──────────────────────────────────────────────────────────
   /agent/$agentId/setup-credentials  (authenticated, owner-only)
        │ lists install's incomplete PBU credentials
        │ user fills each → backend overwrites placeholder, flips
        │ is_placeholder=False, syncs to env
        ▼
   user retries the agent in the same channel; gate now passes.
```

Integration touchpoints with existing features:

| Existing feature | How this plan touches it |
|---|---|
| Agent Bundles (`PublishService._collect_credential_specs`) | Spec emission gains `provided_by`, `publisher_credential_id`, `publisher_ai_credential_id`. |
| Agent Credentials (`Credential.allow_sharing`, `AgentCredentialLink`) | Already supports linking a foreign-owned shareable credential — PBP service credentials reuse this exact path. |
| AI Credentials (`AICredential` + `AICredentialShare`) | PBP AI credentials lean on `AICredentialShare` so foreign installs can link to the publisher's row by reference, never by snapshot. |
| Agent Sessions (`SessionService.initiate_stream`) | Pre-LLM gate inserts before the existing environment-ready check. |
| MCP (`MCPRequestHandler.handle_send_message`) | Same gate, called before `stream_and_collect_response`. |
| A2A (`A2ARequestHandler`) | Same gate, called before message processing. |
| Agent Webhooks (`agent_webhook_service`) | Same gate, called before session creation / script run. |
| Real-time events | New WS events: `INSTALL_SETUP_REQUIRED`, `INSTALL_SETUP_COMPLETED`, `PUBLISHER_CREDENTIAL_BROKEN`. |

---

## 2. Decisions Made (with justification)

### D1. AI credentials by reference, not snapshot

Foreign installs link to the publisher's `AICredential.id` via `AICredentialShare`. The agent-env's existing credential resolution continues unchanged — when it materialises the `.env`, it reads the linked AI credential row at that moment. Snapshotting an API key into the install row would (a) duplicate secret material across N installs, (b) require an immediate distribution of every key rotation, (c) leave revoked keys in storage. A reference is the only sane default. The runtime cost is one extra `AICredential` lookup per env start, which is negligible.

### D2. Per-spec `provided_by` for service credentials, plus per-bundle override knob for AI

Service credentials ship as a list of specs and they may legitimately mix modes inside one bundle (a CRM key shared by the publisher; a personal mailbox provided per user). So `provided_by` lives on the spec.

AI credentials are categorically binary: either the publisher pays, or the user pays. There is no shape where one bundle has "two AI credentials, one publisher-provided and one user-provided" — the env links exactly one conversation and one building credential. So AI provisioning is encoded as two optional bundle-level columns: `publisher_ai_credential_conversation_id`, `publisher_ai_credential_building_id`. NULL means "user provides via the install request" (current behaviour).

The mode is stored on `AgentBundle` (not on the revision) so a publisher can flip "I now provide AI" without re-publishing — flipping a billing bit is governance, not a content change. Existing installs see the new state at next env start because AI credential resolution already happens dynamically.

### D3. Pre-LLM gate is one shared service used by every channel

`InstallReadinessGate` is a static-method service in `backend/app/services/bundles/install_readiness_gate.py`. Every channel calls `gate.check(install_id)` before forwarding to message dispatch. The gate returns a single typed dataclass:

```
GateResult:
  status: "ready" | "needs_setup" | "publisher_broken"
  missing: list[GateMissingItem]   # empty for ready
  setup_url: str | None             # absolute URL, None for ready
  user_message: str                 # pre-formatted markdown for system reply
```

Channel-specific renderers consume `GateResult`:

- **Chat / WebSocket session**: insert a `system`-role `Message` row with `user_message` content; emit a normal stream event so the existing chat UI renders it; do NOT mark `interaction_status=running` (no LLM was called).
- **MCP `send_message`**: bypass `stream_and_collect_response` entirely; return `{response: user_message, context_id, setup_url}` so MCP clients see a clean tool reply instead of a 4xx.
- **A2A**: return a `Message` artifact with `user_message` and a `setup_url` data part; A2A task transitions to `completed` (synthetic) so the caller doesn't get an open task. For SSE responses, the gate result is the only event in the stream.
- **Webhook**: the trigger response body becomes `{status: "needs_setup", setup_url, missing}`; the trigger does NOT create an input task or session.

Gate placement is *between* "session resolution" and "stream handoff". Concretely the spots are:

| Channel | Insertion point |
|---|---|
| Chat (sessions API) | `SessionService.initiate_stream`, immediately before `MessageService.stream_message_with_events`. |
| MCP | `MCPRequestHandler.handle_send_message`, immediately after `get_or_create_mcp_session`, before `stream_and_collect_response`. |
| A2A | `A2ARequestHandler.handle_send_message`, immediately after session resolution, before `MessageService.stream_message_with_events`. |
| Webhook (session-trigger type) | `agent_webhook_service.handle_session_trigger`, before `SessionService.create_session`. |
| Webhook (script-trigger type) | Skipped — script triggers don't talk to the LLM. The publisher's documentation should note that script-trigger agents must not depend on PBU credentials being filled. |

### D4. Setup URL token model — plain JWT, no one-time tokens

The setup page is a normal authenticated route (`_layout`). It reads `agentId` from the URL, fetches the install's incomplete credentials, lets the user fill them in. Authorisation is the standard `CurrentUser` + ownership check — nothing else needed.

One-time tokens were considered and rejected: they add a new short-lived token table, an issuance endpoint, an exchange flow, and a client-side dance, all to solve a problem ("an unauthenticated party should fill in your credentials") that doesn't exist in this product. The only legitimate cross-user setup flow is the publisher pre-filling their own row, which is already covered by PBP. If a future flow needs unauthenticated setup (e.g. e-mail magic link to fill mailbox credentials), it can layer a `SetupAccessToken` table on top without touching the gate.

### D5. Single-page install, no steps

Wizard step counts vary heavily — a fully-PBP bundle has zero credential pickers and the whole wizard is "click Next 4 times". A single page collapses the full surface into one form whose effective height matches the actual content. The header card on the left grounds the user in what they're about to install; the form on the right is the install contract. In the publisher-provides-everything case the right side is mostly informational and Install is one click.

### D6. Auto-prefill is suggestion-only, never auto-commit

The new `GET /catalog/{bundle_id}/install-context` endpoint returns, per spec, a `suggested_credential_id` derived from the user's existing credentials matched by `(type, name)`. The frontend defaults each picker to the suggestion but visibly shows that it's a suggestion (badge + text). The user must keep or change it. The install request encodes `mode` per spec so the backend can distinguish "user explicitly picked this credential" from "user fell back to placeholder".

---

## 3. Single-Screen Install Page Layout

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  [<- Back to catalog]                                                       │
├──────────────────────────────────┬──────────────────────────────────────────┤
│ AGENT HEADER CARD (left, sticky) │  SETUP FORM (right, scrollable)          │
│                                  │                                          │
│  ┌──────────────────────────┐   │  ┌────────────────────────────────────┐  │
│  │  ▣ Bundle icon            │   │  │ AI credentials                     │  │
│  │  Display name             │   │  │ ─────────────                      │  │
│  │  v1.2  ·  publisher@x     │   │  │ ✓ Provided by publisher            │  │
│  │                            │   │  │   (no action needed; billed to    │  │
│  │  Description …             │   │  │    publisher)                     │  │
│  │                            │   │  │   --- OR ---                      │  │
│  │  Required credentials     │   │  │ Conversation: [Default (anthropic)]│  │
│  │   • gmail (imap)  user    │   │  │ Building:     [Default (anthropic)]│  │
│  │   • crm (api_token) pub.  │   │  │  edit ▾                            │  │
│  │   • openai (ai)   pub.    │   │  └────────────────────────────────────┘  │
│  │                            │   │                                          │
│  │  Bundle ID:               │   │  ┌────────────────────────────────────┐  │
│  │  io.opencinna.x.a1b2c3d4  │   │  │ Service credentials                │  │
│  │  Latest revision: rev 7   │   │  │ ─────────────                      │  │
│  │                            │   │  │ ▸ gmail (imap)        user-provided│ │
│  │  [Visit publisher]        │   │  │   We found "gmail-personal" — use? │  │
│  │                            │   │  │   ( ) Use my "gmail-personal"     │  │
│  └──────────────────────────┘   │  │   (•) Skip — set up later (default)│ │
│                                  │  │   ( ) Pick another credential …    │  │
│                                  │  │                                    │  │
│                                  │  │ ▸ crm (api_token)     publisher    │  │
│                                  │  │   ✓ Shared by publisher — no action│ │
│                                  │  │     needed                         │  │
│                                  │  └────────────────────────────────────┘  │
│                                  │                                          │
│                                  │  Right-aligned footer:                  │
│                                  │  [   Install   ]  large, primary button │
└──────────────────────────────────┴──────────────────────────────────────────┘
```

Layout rules:

- Two-column flex on `lg+` breakpoints; stacked on `md` and below (left on top, right below).
- Left card uses `position: sticky; top: header-height` so the agent identity stays visible while the user scrolls the form.
- Right column uses `Accordion` (shadcn/ui) where each spec is one collapsible item. PBP items are collapsed by default and labelled "no action needed". PBU items are collapsed only when an auto-prefill match exists; otherwise expanded.
- The bottom action bar is a single primary `Install` button. No "Next/Back". No step indicator.
- Loading state after click: replace the bar with the same env-progress display the current `WizardStepConfirm` uses, then redirect to the install detail page.

When all specs are PBP and AI is publisher-provided, the right column shows three short paragraphs ("AI provided", "All service credentials provided", "Bundle App Data initialised on first run") and the Install button. That is the intended common case.

---

## 4. Data Model Changes

### 4.1 `AgentBundle` — two new optional columns

| Column | Type | Default | Rationale |
|---|---|---|---|
| `publisher_ai_credential_conversation_id` | UUID FK → ai_credential ON DELETE SET NULL | NULL | Publisher-provided AI for conversation mode. NULL = user provides at install time. |
| `publisher_ai_credential_building_id` | UUID FK → ai_credential ON DELETE SET NULL | NULL | Same, building mode. |

Indexes: none beyond the FKs (low cardinality lookups).

`ON DELETE SET NULL` so deleting the publisher's AI credential degrades the bundle to "user provides" rather than cascading destroy. Foreign installs detect the broken link at runtime and return `publisher_broken` (D7 risks below).

### 4.2 `AgentBundleRevision.required_credential_specs` — new shape

Current shape (per spec):

```
{
  "name": "gmail",
  "type": "email_imap",
  "allow_sharing": false,
  "description": null
}
```

New shape:

```
{
  "name": "gmail",
  "type": "email_imap",
  "allow_sharing": false,           # KEPT — still drives the existing
                                    #        Credential.allow_sharing path
  "description": null,
  "provided_by": "user",            # NEW — "user" | "publisher"
  "publisher_credential_id": null   # NEW — UUID when provided_by="publisher"
}
```

Per-field rationale:

| Field | Why it stays / why it's added |
|---|---|
| `name`, `type` | Unchanged — drives auto-prefill matching and placeholder creation. |
| `allow_sharing` | Kept. Determines whether the publisher's row is even legally shareable when `provided_by=publisher`. The publish-time collector refuses to set `provided_by=publisher` for a credential whose `allow_sharing=false`. |
| `description` | Kept. Surfaced in the install screen accordion ("why this is needed"). |
| `provided_by` | New. Decision point at install time. `"user"` → placeholder. `"publisher"` → link existing publisher row. Default `"user"` keeps backward compatibility. |
| `publisher_credential_id` | New. Stable UUID of the publisher's `Credential` row. Looked up at install time and re-checked at every gate run. Snapshotted into the revision so the revision tells you *exactly* which row to link, even if the publisher later renames their credential. |

Backward compatibility: revisions written before this change have no `provided_by` field. The reader treats missing `provided_by` as `"user"` and missing `publisher_credential_id` as `None`. No data migration needed for existing revisions.

### 4.3 `Credential` — no schema changes

The existing `is_placeholder` flag is exactly what we need for the runtime gate's "incomplete" detection. The existing `allow_sharing` flag is exactly what gates whether a credential can be referenced by a foreign install. The existing path of `AgentCredentialLink` pointing at a foreign-owned shareable credential is exactly the PBP service-credential mechanism. No new columns.

The only invariant we add via service-layer logic: when an install's `AgentCredentialLink` points at a credential whose `owner_id != install.owner_id`, the credential MUST have `allow_sharing=true` AND there MUST exist a `CredentialShare` row from `cred.owner_id` to `install.owner_id`. The publish-time and install-time codepaths already enforce parts of this — we just collect them under one helper.

### 4.4 `AICredentialShare` — no schema changes

`InstallService` materialises an `AICredentialShare` (publisher → installer) on first install of a bundle whose `publisher_ai_credential_*_id` is set. The existing AI credential resolver in environment lifecycle already accepts shared credentials.

### 4.5 No new tables

Specifically rejected:

- A separate `InstallSetupGate` table — the gate is purely a function of the install's existing credential link state. Persisting redundant state would create reconciliation bugs.
- A `SetupAccessToken` table — see D4.
- A `BundleAICredentialReference` table — two FKs on `AgentBundle` is simpler.

---

## 5. `InstallRequest.credentials` Payload — new shape

Current shape (legacy, ambiguous):

```
{
  "credentials": {
    "gmail": "<credential-uuid>",       # link this existing credential
    "crm":   {"api_token": "abc"}       # legacy: stuff this dict into a placeholder
  },
  "ai_credential_selections": { ... }
}
```

New shape (explicit, per-spec mode):

```
{
  "credentials": {
    "gmail": {"mode": "use_existing", "credential_id": "<uuid>"},
    "crm":   {"mode": "placeholder"},                 # default for PBU when user defers
    "mail":  {"mode": "publisher_provides"},          # echoes publisher decision
    "newsl": {"mode": "skip"}                         # treat exactly like placeholder
  },
  "ai_credential_selections": {
    "conversation_credential_id": "<uuid> | null",
    "building_credential_id":     "<uuid> | null",
    "use_publisher_ai":            true               # NEW — explicit ack of PBP AI
  }
}
```

How the frontend signals each case:

| Frontend choice | `mode` value | Backend behaviour |
|---|---|---|
| User picked their existing credential X | `use_existing` + `credential_id` | Reuse legacy "link by UUID" path. |
| Auto-prefill suggestion accepted | `use_existing` + `credential_id` | Same as above. |
| User explicitly chose "set up later" | `placeholder` | Create `is_placeholder=true` empty `Credential`, link it. |
| User left the dropdown alone on a PBU spec with no auto-prefill | `placeholder` (frontend default) | Same as above. |
| Spec is `provided_by=publisher` in the revision | `publisher_provides` (frontend echoes) | Backend ignores frontend value, takes the revision as source of truth, links the publisher's row via existing share path; raises 409 if `allow_sharing=false` (publisher misconfigured). |
| Skip explicitly | `skip` | Equivalent to `placeholder` for now; reserved for a future "don't even link this credential at all" option. |

Validation:

- Backend rejects `use_existing` when `credential_id` doesn't belong to the installer (same as today).
- Backend rejects `use_existing` for a spec whose `provided_by=publisher` (publisher-shared specs are not user-overridable; raises 422 with a friendly message).
- Backend treats omitted spec keys as `mode=placeholder`.
- AI credentials: when the bundle has `publisher_ai_credential_*_id` set, the backend ignores `ai_credential_selections.*_credential_id` and links the publisher's row. `use_publisher_ai` is purely a UI hint that the frontend has acknowledged the publisher-provides state — the backend does not depend on it.

---

## 6. Pre-LLM Gate — Response Shape and Channel Rendering

### 6.1 `GateResult` (internal Python dataclass)

```
GateResult:
  status: Literal["ready", "needs_setup", "publisher_broken"]
  missing: list[GateMissingItem]
  setup_url: str | None     # f"{FRONTEND_HOST}/agent/{install_id}/setup-credentials"
  user_message: str         # pre-rendered markdown, ready to drop into a chat reply

GateMissingItem:
  spec_name: str            # "gmail"
  spec_type: str            # "email_imap"
  reason: Literal["placeholder_empty",
                  "publisher_credential_missing",
                  "publisher_credential_unshared"]
  is_ai: bool               # distinguishes AI from service creds
```

### 6.2 Per-channel rendering

**Chat / WebSocket sessions**

- Insert a `system`-role `Message` row on the session with `content = result.user_message` and `message_metadata = {"setup_url": result.setup_url, "missing": [...]}`.
- Emit one streaming event so the chat UI renders it identically to a normal assistant reply.
- Do not flip `interaction_status` to `running` — the env was never engaged.
- The chat UI's existing markdown renderer turns `[Open setup](url)` into a real link; no new component needed. (Future polish: a dedicated "setup card" component keyed on `message_metadata.setup_url`.)

**MCP `send_message` tool**

- Skip `stream_and_collect_response`.
- Tool return value: `{"response": result.user_message, "context_id": session_id, "setup_url": result.setup_url}`.
- The `setup_url` field is informational; existing MCP clients render the `response` text. The next `send_message` call from the LLM goes through the gate again — once the user fills the placeholder, the gate passes and the MCP session continues. No client-side session restart needed because session continuity is by `context_id`, not by transport.

**A2A**

- Skip `MessageService.stream_message_with_events`.
- Synthesise an A2A `message` artifact containing `user_message` plus a `data` part `{type: "cinna.setup_required", setup_url, missing}`.
- Mark the A2A task as `completed` (synthetic) so callers can't poll forever. Store a marker in `session.session_metadata` so the next inbound message creates a new task instead of reattaching to this one.

**Webhook (session trigger)**

- Skip `SessionService.create_session`.
- Trigger response body: `{"status": "setup_required", "setup_url": "...", "missing": [...]}` with HTTP 200 (semantically not a failure — the webhook fired, the agent just declined to act). Webhook invocation log records `result_state="setup_required"` so the publisher can see it.

**Webhook (script trigger)**

- Skipped from gate. Document the limitation; script triggers must not depend on PBU credentials.

### 6.3 Gate caching

The gate runs on every inbound message. The check is one indexed query `SELECT credential.id, is_placeholder, allow_sharing, owner_id FROM agent_credential_link JOIN credential ON … WHERE agent_id = ?` plus one AI credential lookup. Cheap. No caching layer needed; revisit only if profiling shows it.

When the user fills a placeholder, `CredentialsService.update_credential` already triggers env credential sync. We additionally emit `INSTALL_SETUP_COMPLETED` over WS so the frontend can hide the "setup required" banner without polling.

---

## 7. Backend Implementation

### 7.1 Models

- `backend/app/models/bundles/agent_bundle.py` — add `publisher_ai_credential_conversation_id`, `publisher_ai_credential_building_id` columns and to `AgentBundlePublic`.
- `backend/app/models/bundles/catalog.py`:
  - `InstallRequest.credentials` becomes `dict[str, InstallCredentialSelection] | None`.
  - New `InstallCredentialSelection` Pydantic model with `mode` literal and optional `credential_id`.
  - `AICredentialSelections` gains `use_publisher_ai: bool = False`.
  - New `CatalogInstallContext` response model returned by the install-context endpoint:
    - `bundle: CatalogEntryPublic`
    - `ai_provided_by_publisher: bool`
    - `ai_publisher_credential_summaries: {conversation: {name, type} | null, building: ...}`
    - `service_specs: list[InstallContextSpec]` where each item carries
      `name, type, description, provided_by, publisher_summary, suggested_credential_id, suggested_credential_name`.

### 7.2 Services

**`backend/app/services/bundles/publish_service.py`**

- `_collect_credential_specs` is rewritten to consult, per linked credential:
  1. The publisher install's existing pre-publish bundle-tab override map (Phase 5 — until then, infer from the credential's own state).
  2. If no override and `cred.allow_sharing=true` → emit `provided_by="publisher"` with `publisher_credential_id=cred.id`.
  3. Otherwise → emit `provided_by="user"` with `publisher_credential_id=null`.
- New method `_validate_publisher_provides(session, install)` that asserts every `provided_by="publisher"` spec references a credential where `allow_sharing=true`. Raises a publish-time error with a clear message ("credential `crm` is marked publisher-provided but is not shareable").

**`backend/app/services/bundles/install_service.py`**

- `_setup_install_credentials` is rewritten to use the new spec shape:
  - For each spec, branch on `provided_by`:
    - `"publisher"` → resolve `Credential` by `publisher_credential_id`. If missing or `allow_sharing=false`, log and fall through to placeholder (degraded mode; `last_update_status="degraded"` recorded). Otherwise, ensure a `CredentialShare` exists from publisher to installer (idempotent), then `AgentCredentialLink(install, publisher_credential)`.
    - `"user"` → use the new `InstallCredentialSelection`:
      - `use_existing` → link the user's selected credential (existing path).
      - `placeholder` / `skip` / missing → create a placeholder.
- New helper `_link_publisher_ai_credential(install, bundle)` called in `_install_from_revision` *after* env creation:
  - If `bundle.publisher_ai_credential_conversation_id` is set, ensure `AICredentialShare(publisher, installer, conversation_credential)` exists; pass the credential ID into the env's credential link.
  - Same for building.
  - If both are set, the `InstallRequest.ai_credential_selections` is ignored.

**`backend/app/services/bundles/install_readiness_gate.py`** (NEW)

- `class InstallReadinessGate:`
  - `@staticmethod check(session, install) -> GateResult` — does the placeholder/share validation, returns `GateResult`. Reads `FRONTEND_HOST` to build the setup URL.
  - `@staticmethod missing_for(session, install) -> list[GateMissingItem]` — public API for the install detail page banner ("you have N items to set up").
  - `@staticmethod _format_user_message(missing, setup_url) -> str` — renders the markdown used in `user_message`. Localisation hook: kept simple-string for now; can be templated later.

**`backend/app/services/bundles/catalog_service.py`**

- New `build_install_context(session, bundle, user) -> CatalogInstallContext` — runs the auto-prefill matcher per spec (case-insensitive `(name, type)` match against the user's `Credential` rows + `CredentialShare`s), resolves publisher AI summaries, returns the structured response.

**`backend/app/services/credentials/credentials_service.py`**

- Gain `find_match_for_spec(session, user_id, name, type) -> Credential | None` — used by `build_install_context`. Already half-exists; consolidate.
- `update_credential` flips `is_placeholder=False` whenever the saved data is non-empty (covers the setup page's commit path).

**`backend/app/services/sessions/session_service.py`**

- `initiate_stream` calls `InstallReadinessGate.check(install)` before the existing env-ready check. On non-ready, persists a system message via `MessageService.create_message(role="system", content=user_message)` and returns the gate result up the stack. The streaming endpoint short-circuits to a single SSE event carrying that message.

**`backend/app/mcp/request_handler.py` / `backend/app/mcp/tools.py`**

- `handle_send_message` calls the gate after session resolution. On non-ready, returns `{"response": user_message, "context_id": ..., "setup_url": ...}` directly.

**`backend/app/services/a2a/a2a_request_handler.py`**

- Same pattern; emits the synthesised A2A artifact.

**`backend/app/services/agents/agent_webhook_service.py`**

- Same pattern, but only on session-trigger code path.

### 7.3 API Routes

**Catalog / install**

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/v1/catalog/{bundle_id}/install-context` | NEW. Returns `CatalogInstallContext` (auto-prefill suggestions + publisher AI summary). 404 when bundle not visible. |
| `POST` | `/api/v1/catalog/{bundle_id}/install` | UPDATED to accept the new `InstallCredentialSelection` payload. Backwards-compatibility shim accepts the old `dict[str, str | dict]` shape and converts internally. Shim sunsets in Phase 5. |

**Bundle settings**

| Method | Path | Notes |
|---|---|---|
| `PATCH` | `/api/v1/bundles/{bundle_uuid}` | Already exists. Body extended to accept `publisher_ai_credential_conversation_id` and `publisher_ai_credential_building_id`. Validates ownership of both AI credentials. |

**Install setup page (Phase 4)**

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/v1/agents/{agent_id}/setup-status` | Returns `GateResult` minus `user_message` (frontend renders its own copy). Auth: install owner. |
| `GET` | `/api/v1/agents/{agent_id}/setup-credentials` | Returns the install's incomplete user-provided `Credential` rows (id, name, type, description). Auth: install owner. |
| `PUT` | `/api/v1/agents/{agent_id}/setup-credentials/{credential_id}` | Body: typed credential data. Writes via existing `CredentialsService.update_credential`, flips `is_placeholder=False`, triggers env sync, emits `INSTALL_SETUP_COMPLETED` if the install is now fully ready. |

### 7.4 Configuration / env

No new settings. The setup URL is derived from `settings.FRONTEND_HOST`.

### 7.5 Migrations

| File | Description |
|---|---|
| `add_publisher_ai_credentials_to_agent_bundle.py` | Adds `publisher_ai_credential_conversation_id` and `publisher_ai_credential_building_id` columns to `agent_bundle` with FKs (`ai_credential.id`, `ondelete="SET NULL"`). Downgrade drops both columns. No data backfill. |
| (none for `required_credential_specs`) | The column is JSON; the new fields appear in revisions written after the upgrade. Revisions written before the upgrade are read with safe defaults (`provided_by="user"`, `publisher_credential_id=null`). |

---

## 8. Frontend Implementation

### 8.1 New / replaced files

| File | Status | Purpose |
|---|---|---|
| `frontend/src/routes/_layout/catalog/agents/install/$bundleId.tsx` | UPDATED | Renders `<InstallPage />` instead of `<InstallWizard />`. |
| `frontend/src/components/Install/InstallPage.tsx` | NEW | Two-column layout described in §3. |
| `frontend/src/components/Install/InstallAgentHeaderCard.tsx` | NEW | Left-column sticky header card. |
| `frontend/src/components/Install/InstallSetupForm.tsx` | NEW | Right-column form container; orchestrates AI section + service section + Install button. |
| `frontend/src/components/Install/InstallAICredentialSection.tsx` | NEW | Renders publisher-provides state OR the existing AI picker (refactored from `WizardStepAICredentials`). |
| `frontend/src/components/Install/InstallServiceCredentialItem.tsx` | NEW | One accordion item per spec. Handles auto-prefill display. |
| `frontend/src/components/Install/useInstallContext.ts` | NEW | React Query hook on `["catalog", bundleId, "install-context"]` calling `GET /catalog/{bundle_id}/install-context`. |
| `frontend/src/components/Install/InstallWizard.tsx` and `WizardStep*` | DELETED in Phase 3 | Replaced by the single page. |
| `frontend/src/routes/_layout/agent/$agentId/setup-credentials.tsx` | NEW (Phase 4) | Setup page — full-width form listing only incomplete user-provided credentials. |
| `frontend/src/components/Install/SetupNeededBanner.tsx` | NEW (Phase 4) | Banner on the agent detail page when the gate would fail. Click → setup URL. |
| `frontend/src/components/Agents/AgentBundleTab.tsx` | UPDATED (Phase 5) | New "Credential provisioning" subsection lets the publisher flip `provided_by` per spec and pick AI credentials. |

### 8.2 React Query keys

| Key | Source |
|---|---|
| `["catalog", bundleId, "install-context"]` | `GET /catalog/{bundle_id}/install-context` |
| `["agent", agentId, "setup-status"]` | `GET /agents/{agent_id}/setup-status` |
| `["agent", agentId, "setup-credentials"]` | `GET /agents/{agent_id}/setup-credentials` |

Cache invalidation:

- `PUT /agents/{id}/setup-credentials/{cred_id}` invalidates `["agent", id, "setup-status"]` and `["agent", id, "setup-credentials"]`.
- `INSTALL_SETUP_COMPLETED` WS event → invalidate `["agent", id, "setup-status"]`.

### 8.3 Auto-generated client

Backend changes require regenerating the OpenAPI client: `bash scripts/generate-client.sh` (or `make gen-client`). Plan touchpoints:

- `CatalogService.installBundle` request type changes shape (new `InstallCredentialSelection`).
- New `CatalogService.getInstallContext`.
- New install endpoints under `AgentsService`.

---

## 9. Security

- **Spec validation at publish**: `provided_by="publisher"` requires `allow_sharing=true` on the underlying credential. Enforced server-side; the publisher can't accidentally publish a non-shareable credential as PBP.
- **Install validation**: `mode="use_existing"` requires the credential to be owned by (or shared with) the installer. Existing logic; unchanged.
- **Gate-side validation**: at every channel boundary, re-check that PBP credentials are still owned by the publisher and still `allow_sharing=true`. If a publisher revokes sharing post-install, the gate flips that install to `publisher_broken` until reset (publisher rotates the credential, or installer manually replaces).
- **Setup page authorisation**: only the install owner. No cross-user access.
- **No secrets in URLs / events**: `setup_url` carries only the install ID. The system messages emitted by the gate carry only spec names and credential types — never values.
- **Audit**: gate failures emit `INSTALL_SETUP_REQUIRED` and `PUBLISHER_CREDENTIAL_BROKEN` to the existing event bus; both are visible in the install owner's `Activity` feed and in admin diagnostics.

---

## 10. Risks

### 10.1 Publisher-shared AI credential — risk surface

| Risk | Mitigation |
|---|---|
| Publisher's API key revoked at provider | Gate detects on env start (provider rejects). Publisher gets `PUBLISHER_CREDENTIAL_BROKEN` event; installs receive a synthesised gate reply on next user message until the publisher rotates the key. No silent breakage — every channel surfaces it. |
| Publisher's billing limit hit (noisy-neighbour) | All installs on this bundle fail uniformly until the publisher tops up. Surfaces as a normal LLM-side error today; that error needs to be wrapped at the env layer into a "publisher quota exceeded — contact publisher" system message. Phase 4 stretch goal: detect the provider-specific quota error class in `EnvironmentLifecycleManager` and treat it as a gate-style synthesised reply. |
| Publisher deletes the AI credential row | FK is `ON DELETE SET NULL` on `agent_bundle.publisher_ai_credential_*_id`. Bundle reverts to "user provides"; existing installs become broken (no AI credential resolved). Gate returns `publisher_broken` with a setup URL; the user can supply their own AI credential via the setup page. |
| Publisher's `AICredentialShare` row deleted manually | `_link_publisher_ai_credential` re-asserts the share at every install / env-start; missing share is automatically restored as long as the bundle still references the credential. |
| Cross-instance migration | The publisher's `AICredential.id` is local to this Cinna instance; an export/import of the bundle would need to remap the FK. Out of scope — bundle export/import is not implemented and this constraint should be documented when it is. |

### 10.2 Publisher-shared service credential — risk surface

Inherits all the same patterns; `Credential.allow_sharing` is the existing fuse and the existing `CredentialShare` table is the existing audit row.

### 10.3 Lazy resolution — risk surface

| Risk | Mitigation |
|---|---|
| Gate runs on every message → latency creep | One indexed join per check. Sub-millisecond at our scale. Profile and add caching only if observed. |
| User confused why the agent suddenly "works" again with no chat restart | Gate emits a system message "Setup complete — you can continue" on the WS event. The user retries naturally. Document the behaviour in the setup page success state ("close this tab and return to your chat"). |
| MCP / A2A clients caching the "setup needed" reply as the assistant's voice | The reply is plainly worded and includes a URL. Real risk only if the LLM client embeds it as long-term memory; out of our control. |
| Webhook caller treats setup-required as success | We return HTTP 200 with `status: "setup_required"`. Webhook publishers must inspect the body. Document explicitly. |

### 10.4 Migration risk

- New JSON fields on existing JSON column → no schema migration, no risk of broken old revisions. Old revisions read with `provided_by="user"` default — preserves today's behaviour exactly.
- New `agent_bundle` columns → straightforward; rollback drops the columns.

---

## 11. Phasing

### Phase 1 — Schema & publish-time collection (no UX, no behaviour change)

- Migration: add `publisher_ai_credential_*_id` columns to `agent_bundle`.
- Update `PublishService._collect_credential_specs` to emit the new spec shape inferred from `Credential.allow_sharing` (no UI override yet).
- Update `AgentBundleRevisionPublic` and `CatalogEntryPublic` to surface the new fields.
- Backwards-compat readers: anywhere that reads `required_credential_specs` defaults missing fields.
- Tests: revision created post-upgrade has the new fields; revision created pre-upgrade still installs cleanly.

Rollback: drop new `agent_bundle` columns; the JSON-shape additions are backward-compatible by construction (no migration needed to drop them).

### Phase 2 — Install-time wiring of publisher-shared credentials

- Update `InstallService._setup_install_credentials` to honour `provided_by` and `publisher_credential_id`.
- Add `_link_publisher_ai_credential`.
- Update `PATCH /bundles/{uuid}` to accept the AI credential FKs (no UI yet — superuser-only via API).
- Update `EnvironmentService` AI credential resolution to fall back to the bundle-level publisher AI credential when the install has no override (the resolver already supports shared credentials, but the lookup chain needs the bundle hop added).
- Tests: install of a bundle with PBP AI succeeds; install owner sees no AI credential prompt; env starts using publisher's key.

Rollback: short-circuit `_link_publisher_ai_credential` to no-op; the install still works (falls back to user's defaults).

### Phase 3 — Single-screen install page

- New `InstallPage.tsx` and child components.
- New `GET /catalog/{bundle_id}/install-context`.
- Updated `InstallRequest` payload (with backwards-compat shim).
- Delete `InstallWizard.tsx` and `WizardStep*`.
- Tests: e2e install of a bundle with mixed PBP/PBU credentials and auto-prefill suggestions; e2e install of an all-PBP bundle in one click.

Rollback: keep the shim; the old wizard files can be restored from git if a bug is found post-merge.

### Phase 4 — Pre-LLM gate + setup page + per-channel rendering

- New `InstallReadinessGate` service.
- Wire into `SessionService.initiate_stream` (chat).
- Wire into `MCPRequestHandler.handle_send_message`.
- Wire into `A2ARequestHandler.handle_send_message`.
- Wire into `AgentWebhookService.handle_session_trigger`.
- New `setup-credentials` route + components.
- New endpoints `GET /agents/{id}/setup-status`, `GET/PUT /agents/{id}/setup-credentials`.
- New WS events: `INSTALL_SETUP_REQUIRED`, `INSTALL_SETUP_COMPLETED`, `PUBLISHER_CREDENTIAL_BROKEN`.
- Setup banner on the agent detail page.
- Tests: install with PBU placeholder → first chat message returns gate reply → fill credential → next message succeeds; same for MCP, A2A, webhook (session trigger).

Suggested ordering inside Phase 4: chat first (most users encounter this path), then MCP, then A2A, then webhook. Each is independently shippable behind a single `gate.check` insertion.

Rollback per channel: revert the single `gate.check` call site; the gate service can stay deployed.

### Phase 5 — Bundle-tab UI for publisher to override `provided_by`

- New section on `AgentBundleTab.tsx`: per-spec dropdown (`User provides` | `I provide`) + AI credential pickers (`Publisher provides AI` toggle + two credential dropdowns).
- Persist overrides on the publisher install (a small JSON column `Agent.publish_settings` or similar — TBD; could also be stored on the bundle itself as a publisher preference and mirrored into the next revision).
- Update `PublishService._collect_credential_specs` to consult the override map first, infer second.
- Drop the legacy `InstallRequest.credentials` shim.
- Tests: publisher flips a spec to PBP → next publish writes `provided_by="publisher"` → next install on a foreign user uses the publisher's row.

Rollback: hide the new section behind a feature flag; spec inference falls back to "infer from `allow_sharing`" automatically.

---

## 12. Error Handling & Edge Cases

- **Bundle revision predates schema**: missing `provided_by` → treat as `"user"`, missing `publisher_credential_id` → ignore. Same install path as today.
- **Publisher credential row deleted between publish and install**: install proceeds with a placeholder for that spec; install owner gets a degraded-mode warning toast and a setup banner.
- **Publisher revokes `allow_sharing` after foreign installs exist**: gate flips affected installs to `publisher_broken`; fix is publisher re-enables sharing OR foreign installer manually replaces with own credential.
- **Installer picks an existing credential of wrong type**: backend rejects at install time (`use_existing` validation); no change from today.
- **Gate races with credential update**: race-tolerant — the gate reads the same row that the env reads, both at message time. Worst case the user gets one extra "setup needed" reply right before their fill-in completes; refresh fixes.
- **MCP context_id continuity after gate**: the gate creates the session row but never streams. The `context_id` is valid; the next `send_message` from the LLM goes through the gate again. No special handling needed because session lookup is by UUID, not by interaction history.
- **Gate during agent status pull or other non-LLM commands**: gate only runs on user-message-to-LLM dispatches. Slash commands, status pulls, file ops bypass it.

---

## 13. Out of Scope (for this plan)

- Catalog page redesign.
- Publisher-side dashboards for "how many installs are blocked on setup".
- Cross-instance bundle export/import (PBP keys are local FKs).
- Quota / billing integration for PBP AI credentials beyond surfacing provider errors.
- Setup via unauthenticated magic link.
- Conditional dependencies between specs (e.g. "if user picked OAuth, skip the password spec").
- Setup-required aggregation on the agents list page (could be a follow-up; for Phase 4 we only banner on the install detail page).

---

## 14. Summary Checklist

### Backend tasks

- [ ] Migration: add `publisher_ai_credential_conversation_id`, `publisher_ai_credential_building_id` to `agent_bundle` (FK → `ai_credential.id`, `ondelete=SET NULL`).
- [ ] Update `AgentBundle`, `AgentBundlePublic`, `AgentBundleUpdate` to include the new fields.
- [ ] Add `provided_by` and `publisher_credential_id` to the `required_credential_specs` JSON shape (no schema migration needed); update reader fallbacks.
- [ ] Update `PublishService._collect_credential_specs` and add `_validate_publisher_provides`.
- [ ] Update `InstallService._setup_install_credentials` to branch on `provided_by` and consume the new `InstallCredentialSelection` payload.
- [ ] Add `InstallService._link_publisher_ai_credential` (idempotently materialises `AICredentialShare`).
- [ ] Add `CatalogService.build_install_context` with auto-prefill matcher.
- [ ] Add new `InstallReadinessGate` service in `backend/app/services/bundles/install_readiness_gate.py`.
- [ ] Wire gate into `SessionService.initiate_stream`, `MCPRequestHandler.handle_send_message`, `A2ARequestHandler.handle_send_message`, `AgentWebhookService` (session trigger only).
- [ ] Add endpoints: `GET /catalog/{bundle_id}/install-context`, `GET /agents/{id}/setup-status`, `GET/PUT /agents/{id}/setup-credentials`.
- [ ] Update `PATCH /bundles/{uuid}` to accept publisher AI credential FKs with ownership validation.
- [ ] Add WS events `INSTALL_SETUP_REQUIRED`, `INSTALL_SETUP_COMPLETED`, `PUBLISHER_CREDENTIAL_BROKEN`; emit at appropriate points.
- [ ] Backwards-compat shim in `InstallRequest` for the legacy credentials payload (sunset in Phase 5).

### Frontend tasks

- [ ] Build `InstallPage`, `InstallAgentHeaderCard`, `InstallSetupForm`, `InstallAICredentialSection`, `InstallServiceCredentialItem`.
- [ ] Add `useInstallContext` hook.
- [ ] Replace the wizard route's contents with `<InstallPage />`; delete `InstallWizard` and `WizardStep*`.
- [ ] Build `setup-credentials` route + per-credential form components.
- [ ] Build `SetupNeededBanner` on the install detail page; consume `["agent", id, "setup-status"]`.
- [ ] Subscribe to new WS events; invalidate setup-status query accordingly.
- [ ] Phase 5: extend `AgentBundleTab` with the per-spec `provided_by` editor and the publisher AI pickers.
- [ ] Regenerate API client (`bash scripts/generate-client.sh`) after each backend phase.

### Agent-env tasks

- [ ] Confirm AI credential resolver follows the new fallback chain (install override → publisher AI from bundle → user defaults). Likely zero-change since the resolver already handles shared `AICredential` rows; just verify.
- [ ] No env-side schema or sync change needed for service credentials — `AgentCredentialLink` already drives the existing sync.

### Testing & validation tasks

- [ ] Verify a fully-PBP bundle installs in one click and the env starts with publisher's keys.
- [ ] Verify a mixed-mode bundle (some PBP, some PBU) installs; PBU placeholders are present; first chat message returns the gate reply.
- [ ] Verify auto-prefill suggestion is shown when a matching user credential exists; user can accept or override.
- [ ] Verify gate fires identically across chat, MCP, A2A, webhook (session trigger).
- [ ] Verify setup page commits a placeholder, flips `is_placeholder=False`, and the next message succeeds without restarting the chat / MCP session / A2A task.
- [ ] Verify publisher revoking `allow_sharing` post-install propagates to gate as `publisher_broken`.
- [ ] Verify deleting the publisher's AI credential nulls out `publisher_ai_credential_*_id`; bundle silently degrades to "user provides".
- [ ] Verify revisions written before this change install cleanly.
- [ ] Verify legacy install payload (`credentials: {name: uuid_string}`) still works through the shim.
- [ ] Verify `INSTALL_SETUP_COMPLETED` event clears the setup banner without a manual page refresh.

---

*Plan written: 2026-05-07. Targets `cinna-core` `main` HEAD.*
