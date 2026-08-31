# Agent Improvement Requests — Implementation Plan

> **Output location note.** The `cinna-core.feature.plan` command nominates `docs/drafts/`.
> That directory does not exist in this repo; the two existing plans live in `docs/plans/`.
> This plan follows the repo convention.

## 1. Overview

A user chatting with an agent hits a scenario the agent handled badly. Today that
knowledge is trapped: when the agent is a **bundle install**, the publisher cannot
read the consumer's session (sessions are strictly owner-scoped), so the defect
never reaches the person who can fix it.

**Agent Improvement Requests** add a consent-gated, one-directional data channel:
the *session owner* explicitly shares a **frozen snapshot** of one session plus the
tuning-relevant runtime context with the *agent owner* (the bundle publisher, or
themselves for a standalone agent). The recipient sees the requests in a card on the
agent's Configuration tab, downloads a self-describing ZIP archive, and — via the
`cinna-cli` account workspace — hands the archive to a local coding agent that knows
how to turn it into a fix.

**Core capabilities**

- `/session-improve [comment]` slash command in any session
- **Improve Agent** item in the session page's options menu → consent modal
- `AgentImprovementRequest` row carrying an immutable session snapshot + runtime context
- Publisher-side **Improvement Requests** card on the agent Configuration tab, with
  status workflow (`new` → `in_progress` → `completed` / `declined`)
- Downloadable ZIP archive (README + transcript + structured JSON + context)
- Account-CLI verbs (`cinna improve list|show|download|status`) plus a shipped
  `context/guides/handling-improvement-requests.md` playbook that teaches the local
  coding agent the whole loop — including the bundle-vs-owned-agent distinction

**High-level flow**

```
Consumer (session owner)                    Platform                       Agent owner / publisher
────────────────────────                 ───────────────                  ───────────────────────
/session-improve "it kept    ──▶  resolve_target_agent(source_agent)
 asking for the same file"        ├─ bundle install → publisher install
   or  UI ▸ ⋮ ▸ Improve Agent    └─ else            → the agent itself
            │
            │  consent (submit)          capture frozen snapshot
            └──────────────────▶         + runtime context      ──▶  AgentImprovementRequest
                                         + secret scrub                     (status=new)
                                                                              │
                                                       IMPROVEMENT_REQUEST_CREATED (WS)
                                                                              ▼
                                                              Config tab ▸ Improvement Requests card
                                                                              │
                                                              ┌───────────────┴───────────────┐
                                                              ▼                               ▼
                                                    Download archive (.zip)        cinna improve list/show/
                                                                                    download/status
                                                                                              │
                                                                              context/guides/handling-
                                                                              improvement-requests.md
                                                                                              │
                                                                          fix in publisher install ▸ publish v<next>
```

## 2. Architecture Overview

### 2.1 Components

| Component | Location | Role |
|---|---|---|
| `AgentImprovementRequest` model | `backend/app/models/improvement/agent_improvement_request.py` | Persisted request + frozen snapshot + context |
| `ImprovementRequestService` | `backend/app/services/improvement/improvement_request_service.py` | Create / list / authorize / status transitions |
| `SessionSnapshotService` | `backend/app/services/improvement/session_snapshot_service.py` | Freeze messages + build runtime-context block |
| `SecretScrubber` | `backend/app/services/improvement/secret_scrubber.py` | Mask the source install's credential values out of snapshot text |
| `ImprovementArchiveService` | `backend/app/services/improvement/improvement_archive_service.py` | Build the ZIP in memory from the frozen row |
| `SessionImproveCommandHandler` | `backend/app/services/agents/commands/session_improve_command.py` | `/session-improve` |
| REST routes | `backend/app/api/routes/improvement_requests.py` | Requester + owner surfaces |
| Account-CLI routes | `backend/app/api/routes/cli.py` (`/account/improvement-requests*`) | CLI surface incl. binary archive |
| CLI guide | `backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/guides/handling-improvement-requests.md` | Playbook shipped into `context/guides/` |
| `ImproveAgentModal` | `frontend/src/components/Sessions/ImproveAgentModal.tsx` | Consent modal |
| `ImprovementRequestsCard` | `frontend/src/components/Agents/ImprovementRequestsCard.tsx` | Config-tab card |

### 2.2 Data flow

```
Session (consumer-owned)
   │  capture at consent time only — never read again
   ▼
SessionSnapshotService.capture(session)
   ├─ messages: role, seq, content, timestamp, command flags
   ├─ per-agent-message compact tool digest (derived from streaming_events)
   ├─ attachment descriptors (filename / mime / size — no bytes)
   └─ caps: 50k chars/message, 2 MB total, newest-first retention
   │
   ├─ capture_context()  ─ agent / env / sdk / plugins / prompts (+ divergence)
   └─ capture_memory()   ─ ONE live container read: app-data/memory/*.md
   │                       (opt-out; never wakes a stopped container)
   ▼
SecretScrubber.scrub(snapshot, …) + SecretScrubber.scrub(context, …)
   │
   ▼
AgentImprovementRequest.snapshot  (JSONB, immutable)
AgentImprovementRequest.context   (JSONB, immutable)
   │
   ▼
ImprovementArchiveService.build(request) → bytes (in-memory ZIP, on demand)
```

**Invariant — no live read-through.** The personal-memory read is the single live
read in the feature and it happens *before* the row exists, as part of the consent
action. After the row is written, nothing in this feature ever reads the source
`Session` — or the container — again. The archive is a pure function of
`(snapshot, context, request row, requester projection)`. Deleting or continuing the
session does not change what the recipient sees. This is the whole privacy argument;
any future "refresh snapshot" feature must be an explicit, separately-consented action.

### 2.3 Integration points

- **[Agent Sessions](../application/agent_sessions/agent_sessions.md)** — source of the snapshot; new menu item on the session page
- **[Agent Commands](../agents/agent_commands/agent_commands.md)** — new sync, non-LLM-context command
- **[Agent Bundles](../agents/agent_bundles/agent_bundles.md)** — recipient resolution walks `bundle_uuid` → publisher install; context records installed revision/version
- **[Agent Environments](../agents/agent_environments/agent_environments.md)** — context records env name/version, image tag, SDK, model overrides
- **[Account CLI Workspace](../application/cinna_cli_integration/account_cli_workspace.md)** — new `/account/improvement-requests*` endpoints + guide in the context package
- **[Realtime Events](../application/realtime_events/event_bus_system.md)** — two new `EventType` constants
- **[Agent Credentials](../agents/agent_credentials/agent_credentials.md)** — sensitive-field map reused by the scrubber

## 3. Data Models

### 3.1 `agent_improvement_request`

New table. Model file `backend/app/models/improvement/agent_improvement_request.py`;
re-export from `backend/app/models/__init__.py` (follow the existing pattern — add the
class names to both the import block and `__all__`).

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK, `default_factory=uuid4` | |
| `session_id` | UUID \| None | FK `session.id`, `ON DELETE SET NULL`, indexed | Provenance backlink only. The snapshot outlives the session |
| `source_agent_id` | UUID \| None | FK `agent.id`, `ON DELETE SET NULL` | The install the session ran on (consumer side) |
| `target_agent_id` | UUID | FK `agent.id`, `ON DELETE CASCADE`, NOT NULL, indexed | Receiving agent — publisher install, or the agent itself |
| `bundle_uuid` | UUID \| None | FK `agent_bundle.id`, `ON DELETE SET NULL` | Set when the source install came from a bundle |
| `requester_user_id` | UUID | FK `user.id`, `ON DELETE CASCADE`, indexed | Account deletion removes the shared data — deliberate |
| `owner_user_id` | UUID | FK `user.id`, `ON DELETE CASCADE`, indexed | Denormalised recipient; drives cheap listing + authz |
| `comment` | TEXT \| None | ≤ 4000 chars (API-enforced) | The requester's description of what went wrong |
| `status` | VARCHAR(16) | NOT NULL, default `"new"` | `new` \| `in_progress` \| `completed` \| `declined` |
| `resolution_note` | TEXT \| None | ≤ 2000 chars (API-enforced) | Owner's closing note; **visible to the requester** |
| `source` | VARCHAR(16) | NOT NULL, default `"web_ui"` | `web_ui` \| `command` |
| `snapshot` | JSONB | NOT NULL | Frozen transcript — see §3.2 |
| `context` | JSONB | NOT NULL | Frozen runtime context — see §3.3 |
| `snapshot_message_count` | INT | NOT NULL, default 0 | Cheap list-projection field |
| `snapshot_truncated` | BOOL | NOT NULL, server_default false | True when caps dropped messages |
| `created_at` | TIMESTAMPTZ | NOT NULL | |
| `updated_at` | TIMESTAMPTZ | NOT NULL | |
| `status_changed_at` | TIMESTAMPTZ \| None | | Stamped on every status transition |

**Indexes**

- `ix_air_target_status` btree on `(target_agent_id, status)` — the card query
- `ix_air_owner_created` btree on `(owner_user_id, created_at DESC)` — the CLI cross-agent list
- `ix_air_requester` btree on `requester_user_id` — "my submitted requests"
- `ix_air_session` btree on `session_id` — rate-limit check

**Cascade rationale**

- `target_agent_id` CASCADE — a deleted receiving agent makes the request meaningless
- `session_id` / `source_agent_id` SET NULL — the snapshot is the payload; provenance is best-effort
- `requester_user_id` CASCADE — a user who deletes their account withdraws their shared data
- `owner_user_id` CASCADE — redundant with the agent cascade, but keeps the table clean

**No status enum type.** `status` is a plain `VARCHAR` like `Session.status` and
`AgentEnvironment.status`, so adding a value later needs no migration. Validate in
the Pydantic layer against a module-level `IMPROVEMENT_STATUSES` tuple.

### 3.2 `snapshot` JSON shape

```jsonc
{
  "schema_version": 1,
  "captured_at": "2026-08-19T10:22:31Z",
  "session": {
    "id": "…", "title": "…", "mode": "conversation",
    "status": "active", "result_state": "error", "result_summary": "…",
    "integration_type": null,
    "created_at": "…", "last_message_at": "…",
    "total_message_count": 42
  },
  "messages": [
    {
      "sequence_number": 1, "role": "user", "content": "…",
      "timestamp": "…", "status": "completed",
      "is_command": false, "command_name": null,
      "attachments": [{"filename": "report.pdf", "mime_type": "application/pdf", "size": 12345}]
    },
    {
      "sequence_number": 2, "role": "agent", "content": "…",
      "timestamp": "…", "status": "completed",
      "tool_digest": [
        {"seq": 4, "type": "tool_use", "tool_name": "bash", "brief": "ls /app/workspace/scripts"},
        {"seq": 9, "type": "tool_result", "tool_name": "bash", "brief": "…(truncated)"}
      ]
    }
  ],
  "truncated": false,
  "omitted_message_count": 0
}
```

**Capture rules**

- Ordered by `sequence_number` ascending; all three roles (`user` / `agent` / `system`) included
- `message_metadata.streaming_events` is **never** copied verbatim (it is the largest
  field in the DB and mostly redundant). Instead each agent message gets a
  `tool_digest`: at most **200** entries of `{seq, type, tool_name, brief}`, `brief`
  truncated to **500** chars. Types kept: `tool_use`, `tool_result`, `thinking`
  (first 500 chars), `error`. This is what actually explains "what went wrong"
- Per-message `content` cap: **50,000** chars, tail-truncated with a
  `\n\n…[truncated]` marker
- Total serialized cap: **2 MB**. When exceeded, drop the **oldest** messages first
  (defects cluster at the end of a conversation), set `truncated=true` and record
  `omitted_message_count`
- Attachment **descriptors only** — file bytes are never copied into the snapshot or
  the archive. A publisher does not get the consumer's uploaded files

### 3.3 `context` JSON shape

Everything that plausibly affects tuning, per the requirement.

```jsonc
{
  "schema_version": 1,
  "agent": {
    "source_agent_id": "…", "name": "CRM Agent",
    "is_bundle_install": true, "is_publisher_install": false,
    "bundle_id": "io.opencinna.cinna.a1b2c3d4",
    "installed_revision_number": 7, "installed_version": "1.3",
    "latest_revision_number": 9, "latest_version": "1.5",
    "update_pending": true
  },
  "environment": {
    "env_name": "python-env-advanced", "env_version": "1.0.0",
    "instance_name": "Production",
    "current_image_tag": "…", "expected_image_tag": "…", "image_stale": false,
    "status_at_capture": "running", "critical_state": false, "critical_cause": null
  },
  "sdk": {
    "session_mode": "conversation",
    "agent_sdk_conversation": "opencode/anthropic",
    "agent_sdk_building": "claude-code/anthropic",
    "model_override_conversation": "claude-haiku-4-5",
    "model_override_building": null,
    "effective_engine": "opencode/anthropic",
    "effective_model": "claude-haiku-4-5"
  },
  "plugins": [{"name": "…", "source": "marketplace|bundle", "commit": "…"}],
  "prompts": {
    "schema_version": 2,
    "baseline": "installed_revision", "baseline_version": "1.3", "diverged": true,
    "workflow":       {"chars": 4210, "sha256": "…", "updated_at": "2026-08-12T…",
                       "diverged_from_installed_revision": true,
                       "truncated": false, "text": "…"},
    "entrypoint":     {"…": "…"}, "refiner": {"…": "…"}, "router_trigger": {"…": "…"},
    "sdk_tools": ["Bash", "Read"], "allowed_tools": ["Read"],
    "example_prompts": ["…"]
  },
  "memory": {
    "schema_version": 2,
    "available": true, "unavailable_reason": null, "captured_at": "…",
    "file_count": 2, "total_chars": 3120, "truncated": false,
    "files": [{"filename": "MEMORY.md", "chars": 1200, "sha256": "…",
               "truncated": false, "text": "…"}]
  },
  "recipient": {
    "target_agent_id": "…", "owner_display": "Jane P.",
    "is_shared_externally": true,
    "fallback_reason": null
  },
  "platform": {"captured_at": "…", "frontend_host": "https://…"}
}
```

- `effective_model` must be resolved with the **same** resolver the environment
  configuration uses (model override → credential `default_model` → model-catalog
  tier default). Do not re-implement the precedence; call the existing helper.
- `plugins` comes from the environment's persisted plugin manifest, best-effort —
  a failure to read it records `"plugins": []` and never blocks the request.
- `fallback_reason` is `"publisher_unavailable"` when recipient resolution fell back
  to self (see §5.2), otherwise `null`.

#### `prompts` and `memory` — the system prompt as it ran

Everything else in `context` describes the *settings* of a run. These two describe
the **instructions**, and they exist because a bundle publisher cannot obtain either
from their own install:

- **Prompts drift.** A consumer edits `WORKFLOW_PROMPT.md` in their container; the
  edit flows back into *their* `Agent` row through the bidirectional prompt reconcile
  and stays there. The publisher only ever sees what they published.
- **Memory was never publisher-side.** `app-data/memory/*.md` is injected into every
  system prompt, is excluded from bundle snapshots and git by design, and never
  round-trips to the DB. The container is the only copy.

Rules:

- **Divergence is computed, not guessed.** Each field's text is hashed and compared
  against the same column on the install's `AgentBundleRevision`.
  `diverged_from_installed_revision` is **tri-state**: `null` when there is no
  installed revision, never `false` — a `false` would assert a match that was never
  checked. `baseline` names what was compared against so a reader never infers it.
- **`sha256` is pre-scrub.** It identifies the text as the agent ran it; a `text`
  containing `***REDACTED***` will not re-hash to it, and the archive says so.
- **Caps.** Prompts: 40,000 chars per field, tail-truncated. Memory: 20 files /
  20,000 chars total — the total deliberately mirrors `PERSONAL_MEMORY_MAX_CHARS`
  in `prompt_generator.py`, so the capture can never show more than the runtime
  could inject.
- **Memory is the one live read**, taken once at consent time by
  `SessionSnapshotService.capture_memory` (async; one `exec_command` piping a reader
  script in over stdin) and frozen with everything else. It **never wakes a stopped
  container** — a report must not start billable compute — so `status != "running"`
  short-circuits to `env_not_running`. Post-write the no-live-read-through invariant
  is unchanged.
- **`unavailable_reason` is load-bearing**: `declined_by_requester` / `no_environment`
  / `env_not_running` / `read_failed` / `empty`. "The user opted out", "the container
  was off" and "there were no notes" lead the recipient to different conclusions.
- **Filenames are untrusted.** They come from inside the requester's container, so
  `_safe_member_name` reduces each to a bare basename at capture time — nothing can
  escape `memory/` when the archive is extracted.
- **`schema_version` 1 → 2.** Older rows simply lack both keys. They are **not**
  backfilled: a backfill would read the *current* prompts and memory, which is
  precisely the live read-through this feature forbids.

### 3.4 API schemas (Pydantic, no `table=True`)

- `ImprovementRequestCreate` — `{session_id: UUID, comment: str | None}`
- `ImprovementRequestPublic` — `id, target_agent_id, target_agent_name, source_agent_id, source_agent_name, bundle_id, installed_version, requester_display, requester_email, comment, status, resolution_note, source, snapshot_message_count, snapshot_truncated, created_at, status_changed_at`
- `ImprovementRequestDetailPublic(ImprovementRequestPublic)` — adds `context: dict` (the whole §3.3 block) and `session_title`
- `ImprovementRequestsPublic` — `{data: list[...], count: int}`
- `ImprovementRequestUpdate` — `{status: str | None, resolution_note: str | None}`
- `ImprovementContextPublic` — the modal's pre-flight payload: `{eligible: bool, reason: str | None, is_shared_externally: bool, recipient_display: str, target_agent_name: str, bundle_id: str | None, installed_version: str | None, message_count: int, existing_request_count: int}`

`requester_display` / `requester_email` are shown **only to the recipient**. The
requester's own projection of their submitted requests omits nothing (it is their data).

## 4. Security Architecture

### 4.1 The one cross-user data path

This feature intentionally creates a path where user A's conversation content becomes
readable by user B. Everything below exists to keep that path narrow and honest.

| Control | Rule |
|---|---|
| **Consent is the write** | A request row can only be created by an authenticated user acting on **their own** session. There is no admin, publisher, or automated path that creates one |
| **Frozen at consent** | Post-write, the source session is never read again. The recipient sees exactly what existed at the moment of consent |
| **Explicit disclosure** | The modal names the recipient (publisher display name), the bundle id + version, the capture size, and the irreversibility **inline, above the submit button**. The itemised Included / Not-included lists and the masking caveat sit one click away in a **"Sharing details"** sub-dialog reachable from the footer — off the submit path so the common case is fast, but never further than the button that performs the share. The split is deliberate: what stays inline is what a user cannot be assumed to know (who receives it, that it is final); what moves is the itemisation |
| **Personal memory is opt-out, not implicit** | `app-data/memory` is the requester's own content, not agent configuration, so it gets its own checkbox (default on, since the memory area is part of every system prompt) rather than a line in the Included list. `/session-improve --no-memory` is the chat equivalent. Declining reads nothing from the container at all |
| **No workspace exfiltration** | Prompt *documents* only. Scripts, knowledge files, app data and everything else in the workspace stay put — the Not-included list says so |
| **No live surfaces** | No endpoint in this feature returns a `Session`, `Message`, workspace file, or container log belonging to the requester |
| **No files, no logs** | Attachment descriptors only; container logs are out of scope in v1 (they are per-environment and would leak the requester's *other* sessions) |
| **Secret scrubbing** | Snapshot **and** context text pass through `SecretScrubber` before they are written (§4.2). The context's structural fields are untouched; only the captured prompt and memory documents are rewritten |
| **Audit on read** | Every archive download where `owner_user_id != requester_user_id` writes a `SecurityEvent` |
| **Consent is final** | Per product decision there is no withdrawal. The modal must say so plainly: *"This cannot be undone."* |

### 4.2 `SecretScrubber`

New, self-contained utility. Agents do occasionally echo a token into chat; without
this, a consumer's own API key could ride into a publisher's archive.

- Input: the source install's linked `Credential` rows (via `AgentCredentialLink`),
  decrypted, filtered through `CredentialsService.SENSITIVE_FIELDS` (the existing
  per-type map at `backend/app/services/credentials/credentials_service.py:64` —
  reuse it, do not duplicate the list), plus the linked `AICredential` API keys
- Build a value set; **discard any value shorter than 8 chars** (short values would
  shred ordinary prose)
- Replace each occurrence in every `content`, `brief`, and `result_summary` string
  with `***REDACTED***`; sort the value set by descending length so longer secrets
  match before their prefixes
- Record `scrubbed_hits: int` in `context.platform` for observability. Never log the
  values themselves
- Pure function, no DB access inside — it takes `(payload: dict, secrets: set[str])`.
  Exhaustively unit-testable, following the `assert_url_allowed` / `assert_api_proxy_allowed`
  chokepoint precedent

### 4.3 Authorization matrix

| Endpoint | Requester | Recipient (agent owner) | Anyone else |
|---|---|---|---|
| `GET /sessions/{id}/improvement-context` | ✅ (own session) | — | 404 |
| `POST /improvement-requests` | ✅ (own session) | — | 403 |
| `GET /improvement-requests/mine` | ✅ (own rows) | — | — |
| `GET /agents/{id}/improvement-requests` | — | ✅ (owns agent) | 404 |
| `GET /improvement-requests/{id}` | ✅ | ✅ | 404 |
| `GET /improvement-requests/{id}/archive` | ✅ | ✅ (audited) | 404 |
| `PATCH /improvement-requests/{id}` | ❌ 403 | ✅ | 404 |
| `DELETE /improvement-requests/{id}` | ❌ 403 | ✅ | 404 |

Inaccessible ids return **404, not 403** — matching `assert_can_build`'s
existence-leak-safe convention in the account CLI.

### 4.4 Eligibility gate (submission)

All must hold, else 400 with a specific `reason`:

1. `session.user_id == current_user.id`
2. `session.guest_share_id IS NULL` and `session.webapp_share_id IS NULL` — guests and
   webapp viewers have no identifiable consenting account
3. The session has ≥ 1 message
4. `session.agent_id` resolves to an existing `Agent`
5. Rate limits: ≤ **5** requests per session, ≤ **20** per user per rolling 24 h → 429

A2A/MCP/email-initiated sessions are *not* excluded by type — they are excluded by
rule 1 whenever no human account owns them, and permitted when the owner submits from
the web UI. The `/session-improve` command from an A2A caller fails rule 1 unless the
caller is the session owner.

### 4.5 Input validation

- `comment` — stripped, ≤ 4000 chars, plain text (rendered as text, not markdown, in
  the detail modal to avoid injection into the owner's UI)
- `resolution_note` — stripped, ≤ 2000 chars
- `status` — must be in `IMPROVEMENT_STATUSES`
- Archive filename is derived server-side from the request id; never from user input

### 4.6 Audit

Add to `backend/app/models/events/security_event.py` (free-form `str` constants, no
migration), with a comment block matching the file's existing style:

```
IMPROVEMENT_ARCHIVE_DOWNLOADED = "IMPROVEMENT_ARCHIVE_DOWNLOADED"
```

Written only when `owner_user_id != requester_user_id`. Payload: request id, target
agent id, bundle id, requester user id, acting user id, source IP. Never the content.

## 5. Backend Implementation

### 5.1 API routes — `backend/app/api/routes/improvement_requests.py`

Register in `backend/app/api/main.py`. Tag `improvement-requests` → generated client
service `ImprovementRequestsService`.

| Method | Path | Deps | Response |
|---|---|---|---|
| GET | `/sessions/{session_id}/improvement-context` | `SessionDep`, `CurrentUser` | `ImprovementContextPublic` |
| POST | `/improvement-requests` | `SessionDep`, `CurrentUser` | `ImprovementRequestPublic` (201) |
| GET | `/improvement-requests/mine` | `SessionDep`, `CurrentUser` | `ImprovementRequestsPublic` |
| GET | `/agents/{agent_id}/improvement-requests?status=&skip=&limit=` | `SessionDep`, `CurrentUser` | `ImprovementRequestsPublic` |
| GET | `/improvement-requests/{request_id}` | `SessionDep`, `CurrentUser` | `ImprovementRequestDetailPublic` |
| PATCH | `/improvement-requests/{request_id}` | `SessionDep`, `CurrentUser` | `ImprovementRequestDetailPublic` |
| DELETE | `/improvement-requests/{request_id}` | `SessionDep`, `CurrentUser` | `Message` |
| GET | `/improvement-requests/{request_id}/archive` | `SessionDep`, `CurrentUser`, `Request` | `Response` (`application/zip`) |

The archive route returns a raw `Response` with
`Content-Disposition: attachment; filename="improvement-<first-8-of-id>.zip"` and
**no `response_model`** — mirroring the `/account/api-proxy` raw-passthrough pattern.
Note in the route docstring that the generated TS client handles this as a blob and
the frontend must use the manual-download helper (§6.4).

Path ordering caution: declare `/improvement-requests/mine` **before**
`/improvement-requests/{request_id}` so `mine` is not parsed as a UUID.

### 5.2 `ImprovementRequestService`

```
resolve_target(db, source_agent) -> TargetResolution
    # TargetResolution = (target_agent, owner_user_id, bundle, fallback_reason)
```

1. If `source_agent.bundle_uuid` is set **and** `source_agent.is_publisher_install is False`:
   - load `AgentBundle`; if `publisher_user_id` is not None, look up the publisher
     install: `Agent.bundle_uuid == bundle.id AND Agent.is_publisher_install == True`
   - hit → target = publisher install, owner = `bundle.publisher_user_id`, no fallback
   - miss (publisher install deleted, or ownerless git-imported bundle) → **fall back
     to self**: target = `source_agent`, owner = `source_agent.owner_id`,
     `fallback_reason="publisher_unavailable"`
2. Otherwise (standalone agent, or the publisher's own working install) → target =
   `source_agent`, owner = `source_agent.owner_id`

The fallback is safe by construction: falling back to self never widens who can read
the data.

```
create_from_session(db, session, requester, comment, source) -> AgentImprovementRequest
```

Runs the §4.4 gate, resolves the target, captures snapshot + context, scrubs, writes
the row, emits `IMPROVEMENT_REQUEST_CREATED` to the **owner's** user room. Returns the
row. Raises a typed `ImprovementRequestDenied(reason, message)` that both the route
layer (→ HTTP) and the command handler (→ `CommandResult(is_error=True)`) map.

```
list_for_agent(db, agent_id, owner, status=None, skip, limit) -> (rows, count)
list_for_owner(db, owner, status=None, ...) -> (rows, count)   # CLI cross-agent view
list_for_requester(db, user, ...) -> (rows, count)
get_authorized(db, request_id, user) -> (row, role)            # role ∈ {"owner","requester"}; 404 otherwise
update_status(db, request, owner, status=None, note=None)      # stamps status_changed_at, emits …_UPDATED
delete(db, request, owner)
build_context_preview(db, session, user) -> ImprovementContextPublic
```

`build_context_preview` runs the same gate and the same `resolve_target`, but writes
nothing — so the modal's copy can never disagree with what submission will do.

**N+1 caution.** The list projection needs `target_agent.name`, `source_agent.name`,
requester display/email, and bundle id/version. Resolve them with joined selects or a
single batched id→row map, not per-row `db.get` calls — the same trap flagged in the
user-search assignment projection.

### 5.3 `SessionSnapshotService`

```
capture(db, session) -> tuple[dict, bool, int]        # (snapshot, truncated, message_count)
capture_context(db, session, source_agent, resolution) -> dict          # sync,  DB only
async capture_memory(db, session, source_agent, include=True) -> dict   # async, container
```

- `capture` reads messages ordered by `sequence_number`, applies the §3.2 caps,
  derives `tool_digest` from `message_metadata.streaming_events`, and reads
  attachment descriptors from `message_files` + `file_uploads` (metadata columns only)
- `capture_context` reads the agent, its environment, the bundle + installed/latest
  revisions, the plugin manifest, and the prompt documents + tool configuration;
  every optional lookup is wrapped so a missing piece yields `null` rather than
  aborting the request
- `_prompts_block` diffs each prompt field against the installed
  `AgentBundleRevision` (§3.3). A failed baseline lookup costs only the divergence
  flags, not the prompt text
- `capture_memory` is **deliberately a separate, async method**: it is the only block
  that does not live in the database, so it needs one `AgentEnvConnector.exec_command`
  against the running container. It never raises and never wakes a stopped
  environment; `create_from_session` awaits it and assigns `context["memory"]`

### 5.4 `ImprovementArchiveService`

```
build(request, requester_projection, target_projection) -> bytes
```

In-memory `zipfile.ZipFile(BytesIO(), "w", ZIP_DEFLATED)`. Deterministic contents:

```
improvement-<short-id>.zip
├── README.md               # see below
├── metadata.json           # request row projection + schema_version
├── context.json            # the §3.3 block verbatim
├── prompts/                # only when the row carries prompt text
│   ├── README.md           # divergence table + tool configuration
│   ├── WORKFLOW_PROMPT.md  # named after the workspace docs they mirror,
│   ├── ENTRYPOINT_PROMPT.md#   so a publisher can diff straight against
│   ├── REFINER_PROMPT.md   #   their own install
│   └── ROUTER_TRIGGER_PROMPT.md
├── memory/                 # only when memory was actually captured
│   ├── README.md
│   └── <sanitised app-data/memory/*.md>
└── session/
    ├── messages.md         # human-readable transcript
    └── messages.json       # the §3.2 snapshot verbatim
```

A prompt field with no text is **skipped**, not written empty — an empty
`REFINER_PROMPT.md` reads as "the consumer blanked it" when the truth is "this agent
never had one"; `prompts/README.md` marks it `(not set)` instead.

`README.md` renders, in order:

1. Title: `Improvement request for <agent name>`
2. **What was reported** — the comment verbatim (or *"No comment provided."*)
3. **Who and when** — requester display name + email, submitted timestamp, request id, status
4. **Which agent** — agent name; for bundle installs: bundle id, installed version,
   installed revision number, latest version, whether an update was pending
5. **Runtime context** — a markdown table: session mode, SDK engine, effective model,
   per-mode model overrides, env name/version/instance, image tag + staleness,
   critical state, plugin list
6. **Prompts and memory** — the answer to *"what was the system prompt"*, stated up
   front because it is the first thing a publisher needs and the one thing they
   cannot get from their own install: which documents diverged from the installed
   revision (or that there was no baseline), and either the memory file count or the
   reason no memory was captured
7. **What is in this archive** — file-by-file description, plus explicit notes:
   *container logs are not included*, *uploaded file contents are not included*,
   *workspace files and scripts are not included — only the prompt documents*,
   *credential values were scrubbed from the transcript, the prompts and the memory
   files alike*, and the truncation notice when `snapshot_truncated` is true
8. **How to act on this** — points at `context/guides/handling-improvement-requests.md`
   and states the golden rule for bundle publishers: fix the **publisher install**,
   then publish a new version

Because the archive is a pure function of stored data it is **not cached**; a
2 MB-capped snapshot makes on-demand generation cheap. No filesystem writes at all —
which deliberately avoids introducing a new write path that would need a
docker-compose volume mount to survive deploys.

### 5.5 `/session-improve` command

`backend/app/services/agents/commands/session_improve_command.py`

```python
class SessionImproveCommandHandler(CommandHandler):
    streams = False
    include_in_llm_context = False          # meta-command; the LLM must not see it
    requires_running_environment = False    # never wakes a container
    name = "/session-improve"
    description = "Share this session with the agent's owner to help improve it"
```

`execute(context, args)`:

- `args` (trimmed) is the comment
- Load the session; verify `context.user_id == session.user_id`
- Call `ImprovementRequestService.create_from_session(..., source="command")`
- Success → markdown confirmation that **names the recipient explicitly**, e.g.
  *"Improvement request submitted. A copy of this conversation was shared with
  **Jane P.**, publisher of `io.opencinna.cinna.a1b2c3d4` (v1.3)."* — or, when
  `is_shared_externally` is false, *"Improvement request created on your own agent.
  Nothing was shared outside your account."*
- Denied → `CommandResult(content=<reason>, is_error=True)`

Register in `backend/app/services/agents/commands/__init__.py`.

**Autocomplete availability.** In `CommandService.list_for_session`, mark
`/session-improve` `is_available=False` when the session is a guest or webapp share
(`guest_share_id` / `webapp_share_id` set) — the same shape as the existing
`/rebuild-env` availability check. Add the command row to the table in
`docs/agents/agent_commands/agent_commands.md`.

### 5.6 Events

`backend/app/models/events/event.py`:

```python
# Improvement requests — a session owner shared a session with the agent's owner
# (bundle publisher, or themselves). Emitted to the RECIPIENT's user room so the
# Configuration-tab card badge updates live. Meta carries `request_id`,
# `target_agent_id`, `source_agent_id`, `bundle_uuid`, `status`.
IMPROVEMENT_REQUEST_CREATED = "improvement_request_created"
IMPROVEMENT_REQUEST_UPDATED = "improvement_request_updated"
```

No email in v1 (product decision). No Notification Catalog entry — if that changes
later, it is a catalog entry + template, no migration.

### 5.7 Account-CLI routes

Added to `backend/app/api/routes/cli.py`, authenticated by `AccountCLIContextDep`,
delegating to the same service (so ownership rules cannot drift):

| Method | Path | Notes |
|---|---|---|
| GET | `/account/improvement-requests?status=&agent_id=&limit=` | Cross-agent list for everything the account user owns |
| GET | `/account/improvement-requests/{id}` | Detail incl. `context` |
| GET | `/account/improvement-requests/{id}/archive` | Binary ZIP — a **dedicated** route because the JSON-only `api-proxy` cannot carry a binary body (same reason `/account/files/upload` exists) |
| PATCH | `/account/improvement-requests/{id}` | `{status, resolution_note}` |

The `improvement-requests` prefix is *not* on the api-proxy denylist, so `cinna api`
would also reach the JSON endpoints — the dedicated routes exist for ergonomics and
for the archive. No denylist change is required; do **not** add one.

Audit: the CLI archive route writes the same `IMPROVEMENT_ARCHIVE_DOWNLOADED`
`SecurityEvent` as the web route.

## 6. Frontend Implementation

### 6.1 Session page menu item + consent modal

`frontend/src/routes/_layout/session/$sessionId.tsx` — add above `EditSession` in the
existing `DropdownMenuContent`:

```tsx
<ImproveAgentMenuItem session={session} onSuccess={() => setMenuOpen(false)} />
```

`frontend/src/components/Sessions/ImproveAgentMenuItem.tsx` follows the
`EditSession.tsx` pattern exactly (a `DropdownMenuItem` that owns its dialog state).

`frontend/src/components/Sessions/ImproveAgentModal.tsx`:

- `useQuery(["improvementContext", session.id], …)`, enabled only while open
- **Loading** → skeleton rows
- **`!eligible`** → a muted explanation and a single Close button (no form)
- **One header info block** carries both orienting sentences at a single type
  size — the recipient line and the capture-size line (`N messages will be
  captured as they are right now…`, plus the already-submitted count). As two
  paragraphs at `text-sm` and `text-xs` in two places they read as unrelated
  notes at jumping sizes. Filled but border-less with a leading `Info` icon, so
  it cannot be mistaken for the `Textarea` below; rendered through
  `DialogDescription asChild` so the visible block *is* the dialog's
  `aria-describedby` target rather than a decorative div beside a hidden one.
  Agent and recipient names render as `Badge variant="secondary"` (a `<span>`,
  therefore valid inside the `<p>`).
- **The recipient line names the resolved recipient**, because the three
  outcomes are different actions and one generic line described none of them.
  The pre-flight has already resolved the recipient, so the header states it
  rather than hedging:
  > *bundle install* — Goes to **Jane P.**, who publishes the bundle this agent
  > was installed from.
  > *shared agent* — Goes to **Jane P.**, who owns this agent.
  > *own agent* — Kept on your own agent **Friendly Chap** as a note for later
  > improvements. Nothing leaves your account.
  > *pre-flight running, or ineligible* — Report what went wrong so it can be
  > fixed. (No recipient is resolved yet, so promise nothing about one.)
- **Eligible + `is_shared_externally`** → an amber `Alert`-style callout carrying
  only what the description cannot — the bundle coordinates and the finality:
  > Installed from bundle **`io.opencinna.cinna.a1b2c3d4` v1.3**. Submitting
  > shares a copy of this conversation's messages. **This cannot be undone.**
- **Eligible + not shared** → **no callout at all.** Its content is the header
  block now, and a bordered muted panel sitting directly above the `Textarea`
  read as a second input rather than as a notice.
- The `<form>` sets `grid gap-4`. It is a single grid child of `DialogContent`,
  so the dialog's own `gap-4` never lands between header, body and footer;
  without it the checkbox sits flush against its neighbours. The loading /
  error / ineligible branches therefore carry **no** `py-*` of their own — that
  padding existed only to fake the missing gap and now doubles it.
- A **"Sharing details"** ghost button in the footer, left-aligned beside Cancel /
  Submit, opens a `SharingDetailsDialog` carrying the two-column list —
  *Included:* messages in this session, agent + environment + model settings, the
  agent's instructions (its prompt documents) and which tools it may use.
  *Not included:* credential values, uploaded file contents, the agent's
  scripts and knowledge base, container logs, your other sessions. This list
  deliberately claims nothing about `app-data/memory`; instead the dialog renders
  a **live** memory row driven by the form's checkbox, so it describes the
  submission the user is actually about to make rather than a fixed one.
  The trigger must be `type="button"` — the shared `Button` sets no type, so
  inside the form it would default to submit and fire the request. The
  sub-dialog's footer is a single outline **Close** button.
- Always: an `includeMemory` checkbox, **checked by default** — *"Include MEMORY
  files of this agent"*. Personal memory is the only captured block that is the
  requester's own content rather than agent configuration, so it gets a decision
  rather than a bullet; and it defaults on because the memory area is injected into
  every system prompt, so a recipient debugging without it is debugging the wrong
  prompt. Unchecking it means nothing is read from the container at all. The
  explanation of *what* those files are lives on the Sharing details row that
  mirrors this checkbox, not under the label — one line, no helper paragraph.
- `comment` — `Textarea`, optional, `maxLength={4000}`, live counter, label
  *"What went wrong? (optional)"*, placeholder taken from the requirement's example
- Submit button label: **"Share & submit"** when external, **"Submit"** otherwise;
  disabled while pending
- `useMutation` → on success: `toast.success`, close, `queryClient.invalidateQueries`
  on `["improvementRequests"]`; on error: surface the API `detail`

Form uses `react-hook-form` + `zod` per project convention — it keeps the max-length
message consistent with the rest of the app and carries the `includeMemory` boolean
through to `ImprovementRequestCreate.include_memory`.

### 6.2 Configuration-tab card

`frontend/src/components/Agents/ImprovementRequestsCard.tsx`, added to
`AgentConfigTab.tsx` as a **bare `<Card>`** direct child of the existing responsive
grid (the file's comment is explicit that anything else strands a neighbour on its
own row).

Visibility: render only when the current user owns the agent — i.e. skip entirely when
`readOnly` is true (foreign-install view). Do **not** gate on `showOperationalSettings`:
a plain `agent-user` who owns a standalone agent should still see requests on it.
The card is also not gated by the developer role — receiving feedback on an agent you
own is an owner capability, not a developer one.

Layout modelled on `McpConnectorsCard.tsx`:

- `CardHeader` — title **"Improvement Requests"**, description
  *"Feedback from people who used this agent, with the session that triggered it."*,
  and a right-aligned status filter (`Select`: All / New / In progress / Completed /
  Declined, defaulting to **New**, with the new-count in the label)
- `CardContent` — compact table: **Requester**, **Comment** (single-line ellipsis),
  **Version** (the install's bundle version at capture, or `—`), **Date**, **Status** badge
- Row click → detail modal
- Empty state — *"No improvement requests yet. Users can send one from a session's
  ⋮ menu or with `/session-improve`."*
- Query key `["improvementRequests", agent.id, status]`; invalidated by the
  `improvement_request_created` / `_updated` WebSocket events via the existing event
  subscription hook

Status badge colours, matching the platform's task-status conventions:
`new` violet · `in_progress` blue · `completed` green · `declined` muted/gray.

### 6.3 Detail modal

`frontend/src/components/Agents/ImprovementRequestDetailModal.tsx`:

- Header: requester display name + email, submitted date, request short-id
- **Comment** — rendered as plain text in a bordered block (not markdown)
- **Context summary** — a definition list: agent, bundle id + installed version,
  update pending, session mode, SDK engine, effective model, env name/version,
  image staleness, critical state, plugin count. `snapshot_truncated` shows an
  inline amber note
- **Status** — `Select` bound to a `PATCH` mutation, plus a `resolution_note`
  `Textarea` with a Save button. Copy under the note: *"Visible to the person who
  submitted this request."*
- **Download session archive** — primary button (§6.4)
- **Delete** — destructive, with an `AlertDialog` confirm

### 6.4 Binary download helper

The generated OpenAPI client is awkward with binary responses. Reuse whatever the
environment panel / file download already does (`files/{id}/download`); if that is a
manual `fetch` with the `access_token` bearer header plus an object-URL anchor, put
the shared version in `frontend/src/utils.ts` (or the existing download util) and use
it from both places rather than adding a second copy.

### 6.5 Client regeneration

After the backend routes land:

```bash
source ./backend/.venv/bin/activate && make gen-client
```

Then typecheck only the touched files:

```bash
cd frontend && npx tsc --noEmit 2>&1 | grep -E "(ImproveAgent|ImprovementRequest|sessionId)" | head -20
```

## 7. Database Migration

Generate with `make migration` (Docker), then hand-edit.

- **Revision message**: `add agent_improvement_request table`
- **Creates**: `agent_improvement_request` (§3.1) with all four indexes
- **Modifies**: nothing — no existing table changes, no backfill
- **Server defaults**: `status` → `'new'`, `source` → `'web_ui'`,
  `snapshot_message_count` → `0`, `snapshot_truncated` → `false`; `snapshot` and
  `context` → `'{}'::json` (mirrors `AgentBundleRevision.manifest`)
- **Foreign keys**: as tabulated in §3.1, with the stated `ondelete` behaviours
- **Downgrade**: `op.drop_index(...)` ×4 then `op.drop_table("agent_improvement_request")`

Sanity-check that autogenerate did not also pick up unrelated drift before committing
the file.

## 8. CLI Guide (Knowledge Repository Format)

New file:
`backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/guides/handling-improvement-requests.md`

`ContextPackageService` globs `knowledge/guides/**` into `context/guides/`, so the file
needs no registration. **Do** update the generated `context/README.md` index string in
`backend/app/services/cli/context_package_service.py` (the `guides/` row and the
"worked walkthroughs" sentence) so the orchestrator is pointed at it.

Guide outline — this is the "special extra file of instructions" from the requirement:

1. **When to use this** — trigger phrases like *"check for improvement requests and
   implement those."*
2. **Discover** — `cinna improve list --status new`
3. **Read** — `cinna improve show <id>`, then `cinna improve download <id>`; read
   `README.md` first, then `session/messages.md`. Write a short summary of *expected
   vs. actual* before touching anything.
4. **Establish ownership** — check the target agent's flags (`cinna account agents`
   shows `is_foreign_install`; the archive's `context.json` carries
   `is_publisher_install`, `bundle_id`, `installed_version`):
   - **Standalone agent you own** → fix in the synced workspace, finalize per
     `authoring-agent-prompts.md`. Done.
   - **Publisher install of a bundle you publish** → fix here, verify, then tell the
     user to **publish a new version** from the Bundle tab. Explain the consequence:
     automatic-mode installs converge on their own; manual-mode installs need the
     owner to click Update. Never attempt to edit a consumer's install.
   - **A consumer install you own** (the publisher was unavailable, so the request
     landed on your own copy) → any local change is **overwritten by the next bundle
     update**. Warn the user and suggest forwarding the feedback to the publisher instead.
5. **Decide autonomy.** Implement immediately only when *all* hold: the change is
   clearly within the agent's existing purpose; it is localized to prompts or scripts
   already in the workspace; it touches no credentials, external systems, or the
   agent's published contract (A2A skills, `agent_api` endpoints, schedules, bundle
   credential specs). Otherwise **stop and ask the user** — anything that deviates
   from the agent's stated purpose, is ambiguous, is disproportionately large, or
   changes a contract needs approval.
6. **Close the loop** — `cinna improve status <id> completed --note "…"` (or
   `declined` with the reason). The note is shown to the requester.
7. **Handling the data** — the archive is another person's conversation. Do not copy
   it into the agent's workspace, do not commit it, do not paste any value that looks
   like a secret anywhere, and delete the local copy when finished.

### CLI verb contract (implemented in the separate `cinna-cli` repo)

| Command | Backend endpoint | Behavior |
|---|---|---|
| `cinna improve list [--status S] [--agent A]` | `GET /api/v1/cli/account/improvement-requests` | Table: id, agent, requester, version, date, status |
| `cinna improve show <id>` | `GET …/improvement-requests/{id}` | Full detail incl. context block |
| `cinna improve download <id> [--out DIR]` | `GET …/improvement-requests/{id}/archive` | Save + extract into `improvements/<short-id>/`; print the path |
| `cinna improve status <id> <status> [--note N]` | `PATCH …/improvement-requests/{id}` | Set status / resolution note |

## 9. Error Handling & Edge Cases

| Scenario | Behavior |
|---|---|
| Session has no messages | 400 `empty_session` — modal shows the reason, submit hidden |
| Guest / webapp-share session | 400 `not_eligible`; command marked unavailable in autocomplete |
| Caller is not the session owner | 403 (command → error `CommandResult`) |
| Session's agent was deleted | 400 `agent_missing` |
| Publisher install deleted / ownerless git bundle | Falls back to self-target; `fallback_reason` recorded; modal copy switches to the non-shared variant |
| Environment absent or suspended at capture | Context fields are `null`/`status_at_capture:"absent"`; capture never blocks |
| Plugin manifest unreadable | `plugins: []`, warning logged, request still created |
| Installed bundle revision missing | `prompts.baseline: "none"`, every `diverged_from_installed_revision` is `null` (**not** `false`), prompt text still captured |
| Environment stopped at capture | `memory.available: false`, `unavailable_reason: "env_not_running"`. The container is **not** woken — a report must never start billable compute |
| Memory read times out / container unreachable | `unavailable_reason: "read_failed"`; the archive tells the recipient to assume memory may still have been in play |
| Requester unchecked "include my notes" | `unavailable_reason: "declined_by_requester"`; nothing is read from the container at all |
| Hostile memory filename (`../../etc/passwd`) | Reduced to a bare basename by `_safe_member_name` at capture time; post-sanitising collisions are disambiguated, never overwritten |
| Pre-existing request row (`context.schema_version` 1) | Rendered without the prompts/memory sections in both the archive and the detail modal. Never backfilled — a backfill would read *current* prompts and memory, i.e. the live read-through this feature forbids |
| Snapshot exceeds 2 MB | Oldest messages dropped, `snapshot_truncated=true`, stated in README and in the detail modal |
| Rate limit hit | 429 with a human message naming the limit |
| Target agent deleted after submission | Row cascade-deleted with the agent |
| Requester account deleted | Row cascade-deleted — the shared data goes with them |
| Archive requested for a row whose snapshot is `{}` (legacy/corrupt) | 500-safe: build an archive containing README + metadata + an explicit `snapshot unavailable` note rather than raising |
| Concurrent status edits | Last write wins; `status_changed_at` reflects the last transition. No optimistic locking (single-owner surface) |
| WebSocket down | Card falls back to normal React Query refetch on mount/focus |

## 10. UI/UX Considerations

- **Status colours** — `new` violet, `in_progress` blue, `completed` green,
  `declined` muted. Consistent with the task-status badges already used in the
  session header's sub-task counts.
- **Honesty over friction** — the modal's disclosure block is the feature's core UX.
  It must name a real person or org (publisher display name), the concrete bundle
  version, and the irreversibility, before the button. No dark patterns.
  The one pre-checked control is the personal-memory checkbox, and it is pre-checked
  because unchecking it degrades the report rather than protecting anything the
  Included list already covers — its label and helper text say exactly what it
  shares and that unchecking is available.
- **Empty states** — the card's empty state teaches both entry points (`⋮` menu and
  `/session-improve`), so an owner learns how to ask users for feedback.
- **New-count affordance** — the status filter's "New" option shows the count; the
  card header shows a small badge when `new > 0` so the Configuration tab signals
  pending work without an email.
- **Command discoverability** — `/session-improve` appears in the existing
  autocomplete popup with its description; no separate onboarding needed.
- **Copy-to-clipboard** — the detail modal's request short-id is copyable, so an owner
  can paste it straight into `cinna improve show <id>`.
- **Accessibility** — the disclosure callout uses `role="note"`, not colour alone;
  status badges carry text labels; the table rows are keyboard-activatable buttons.

## 11. Integration Points & Rollout Notes

- **No agent-env change.** Nothing in this feature touches the container image or
  `/app/core` — so, unlike the MCP-provider work, there is **no environment rebuild
  requirement** and no stale-template trap. The personal-memory capture deliberately
  goes through the **existing** `/exec` endpoint with a reader script piped in over
  stdin, rather than a new env-core route, precisely so it works against containers
  built before this feature landed.
- **No new filesystem write path**, so no docker-compose mount to add. This was a
  deliberate design choice (snapshots in JSONB, archive built in memory) precisely to
  avoid the "new write path silently unmounted" class of deploy defect.
- **Client regeneration is required** after the backend routes land (§6.5).
- **Context package** picks the guide up automatically once the file exists in the
  `platform-knowledge-env` template; only the generated index string needs editing.
- **Docs to update on completion** (feature-documenter's job, not the developer's):
  new feature row in `docs/README.md`'s **application** registry, a glossary entry for
  *Improvement Request*, the command table in
  `docs/agents/agent_commands/agent_commands.md`, an integration-point line in
  `docs/application/agent_sessions/agent_sessions.md` and
  `docs/agents/agent_bundles/agent_bundles.md`, and the CLI verb tables in
  `docs/application/cinna_cli_integration/account_cli_workspace.md`.

## 12. Future Enhancements (Out of Scope)

- **Container-log attachment** — deferred deliberately: env logs are per-environment
  and would leak the requester's other sessions. A future version would need
  per-session log correlation in env-core first.
- **Withdrawal / retention expiry** — the product decision is that consent is final.
  If revisited, the shape is a `withdrawn` status plus a nulled `snapshot`, and a
  retention sweeper — the schema already tolerates both.
- **Snapshot refresh** — a second, separately-consented capture appended to the same
  request.
- **Email / notification-catalog entry** — a `improvement_request_created` catalog
  type would be additive (enum value + catalog entry + template, no migration).
- **Aggregate view for publishers** — a bundle-level roll-up across all installs, and
  clustering of similar reports via an AI function.
- **Requester follow-up thread** — a comment exchange on a request; today the only
  reply channel is the one-shot `resolution_note`.
- **Direct file attachment** — letting the requester attach a screenshot to the request.
- **Auto-open as an Input Task** on the publisher's side.

## 13. Summary Checklist

### Backend

- [ ] Create `backend/app/models/improvement/agent_improvement_request.py` with
      `AgentImprovementRequest` (§3.1) + the Pydantic schemas (§3.4); re-export from
      `app/models/__init__.py` (import block **and** `__all__`)
- [ ] Add `IMPROVEMENT_STATUSES` tuple + `IMPROVEMENT_SOURCE_*` constants
- [ ] Generate + hand-edit the Alembic migration (§7); verify no unrelated drift
- [ ] Implement `SessionSnapshotService.capture` with the §3.2 caps and `tool_digest`
      derivation
- [ ] Implement `SessionSnapshotService.capture_context` (§3.3), reusing the existing
      effective-model resolver
- [ ] Implement `_prompts_block` with **computed** divergence against the installed
      `AgentBundleRevision`, tri-state (`null` when there is no baseline)
- [ ] Implement `async capture_memory` — one `exec_command` with the reader piped in
      over stdin, never wakes a stopped container, never raises, filenames sanitised
      at capture time
- [ ] Implement `SecretScrubber` as a pure function (§4.2), reusing the credentials
      sensitive-field map; add `text` to `SCRUBBED_KEYS` and run it over the
      **context** as well as the snapshot
- [ ] Implement `ImprovementRequestService` (§5.2) incl. `resolve_target`, the
      eligibility gate, rate limits, and `ImprovementRequestDenied`
- [ ] Implement `ImprovementArchiveService.build` + the README renderer (§5.4)
- [ ] Add `backend/app/api/routes/improvement_requests.py` (§5.1); register in
      `app/api/main.py`; declare `/mine` before `/{request_id}`
- [ ] Add `IMPROVEMENT_ARCHIVE_DOWNLOADED` to `security_event.py` and write it on
      cross-user archive downloads (web **and** CLI routes)
- [ ] Add `IMPROVEMENT_REQUEST_CREATED` / `_UPDATED` to `EventType`; emit to the
      recipient's user room
- [ ] Add `SessionImproveCommandHandler`; register in `commands/__init__.py`
- [ ] Add the guest/webapp availability rule for `/session-improve` in
      `CommandService.list_for_session`
- [ ] Add the four `/account/improvement-requests*` routes to `cli.py` (§5.7),
      delegating to the shared service
- [ ] Add `knowledge/guides/handling-improvement-requests.md` to the
      `platform-knowledge-env` template; update the `context/README.md` index string
      in `context_package_service.py`

### Frontend

- [ ] `ImproveAgentMenuItem.tsx` + wire into the session-page dropdown
- [ ] `ImproveAgentModal.tsx` with the pre-flight context query, the header info
      block (recipient + capture size, one type size, names as badges), the
      external-only amber callout, and the default-on `includeMemory` checkbox (§6.1)
- [ ] `SharingDetailsDialog` in the same file for the Included / Not-included lists
      and the masking caveat, opened from the footer; memory row driven by the live
      checkbox; trigger `type="button"`; `detailsOpen` cleared on close (§6.1)
- [ ] Separator above **Delete session** in the session page's `⋮` dropdown, so the
      destructive item does not read as one more entry beside Edit
- [ ] `ImprovementRequestsCard.tsx` as a bare `<Card>` in the `AgentConfigTab` grid,
      owner-only, with the status filter and compact table (§6.2)
- [ ] `ImprovementRequestDetailModal.tsx` with status select, resolution note,
      download, and delete (§6.3), plus the prompts-diverged (tri-state) and
      personal-memory summary rows
- [ ] Shared authenticated binary-download helper; reuse it for the existing file
      download rather than duplicating (§6.4)
- [ ] Subscribe the card to the two new WebSocket events for live invalidation
- [ ] Regenerate the API client; targeted `tsc --noEmit` check on the touched files

### Agent-env

- [ ] None. Explicitly verify no `/app/core` or env-template change is needed, so no
      rebuild is required.

### Testing & validation

- [ ] A consumer install of a published bundle produces a request that lands on the
      **publisher install**, visible to the publisher and not to anyone else
- [ ] A standalone agent produces a self-targeted request; the modal shows the
      non-shared copy
- [ ] A bundle whose publisher install was deleted falls back to self-target with
      `fallback_reason="publisher_unavailable"`
- [ ] Guest-share and webapp-share sessions are rejected, and the command is marked
      unavailable in the autocomplete list
- [ ] A non-owner cannot read, download, patch, or delete another user's request
      (404, not 403)
- [ ] Continuing the conversation after submitting does **not** change what the
      archive contains
- [ ] Deleting the source session leaves the request and archive intact
- [ ] A credential value echoed into a message is `***REDACTED***` in the archive
- [ ] A session exceeding the 2 MB cap truncates oldest-first and flags
      `snapshot_truncated` in the row, the modal, and the README
- [ ] Rate limits return 429 at 6 requests on one session and at 21 in 24 h
- [ ] The archive opens as a valid ZIP with all base members, and `context.json`
      carries bundle version, installed/latest revision, session mode, SDK engine,
      and effective model
- [ ] A consumer install whose `WORKFLOW_PROMPT.md` was edited after install reports
      `diverged: true` for that field only, and ships the edited text in
      `prompts/WORKFLOW_PROMPT.md`
- [ ] A standalone agent (no installed revision) reports divergence as `null` and the
      archive says there was no baseline — not "matches"
- [ ] Memory files land under `memory/` with sanitised names; a `../` filename cannot
      escape the directory on extraction
- [ ] Submitting with the memory checkbox cleared performs **no** container read and
      records `declined_by_requester`
- [ ] Submitting against a stopped environment records `env_not_running` and does not
      start the container
- [ ] A credential value echoed into a prompt document or a memory file is
      `***REDACTED***` in the archive
- [ ] Cross-user archive download writes exactly one `SecurityEvent`; a same-user
      download writes none
- [ ] Status transitions stamp `status_changed_at` and emit the update event
- [ ] Deleting the target agent cascade-deletes its requests; deleting the requester's
      account cascade-deletes theirs
- [ ] `GET /account/improvement-requests` returns rows across all agents the account
      user owns, and the CLI archive route returns a valid binary ZIP
