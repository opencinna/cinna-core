# Agent Improvement Requests — Technical Details

See [business logic](agent_improvement_requests.md) for the product-level model. This document covers models, routes, services, and frontend implementation.

## File Locations

### Backend

**Models:**
- `backend/app/models/improvement/agent_improvement_request.py` — `AgentImprovementRequest` (table) + Pydantic schemas (`ImprovementRequestCreate`, `ImprovementRequestPublic`, `ImprovementRequestDetailPublic`, `ImprovementRequestsPublic`, `ImprovementRequestUpdate`, `ImprovementContextPublic`); `IMPROVEMENT_STATUSES` / `IMPROVEMENT_SOURCES` tuples; re-exported from `app/models/__init__.py`

**Routes:**
- `backend/app/api/routes/improvement_requests.py` — requester + recipient surfaces (`/sessions/{id}/improvement-context`, `/improvement-requests*`, `/agents/{id}/improvement-requests`); registered in `backend/app/api/main.py`
- `backend/app/api/routes/cli.py` — the `/account/improvement-requests*` block (account-CLI transport, same service)

**Services:**
- `backend/app/services/improvement/improvement_request_service.py` — `ImprovementRequestService`: target resolution, eligibility gate, rate limits, create/list/get/update/delete, projections, event emission
- `backend/app/services/improvement/session_snapshot_service.py` — `SessionSnapshotService`: transcript capture (`capture`) and runtime-context capture (`capture_context`)
- `backend/app/services/improvement/secret_scrubber.py` — pure-function `scrub(payload, secrets) -> (payload, hits)`, plus `collect_credential_secrets`
- `backend/app/services/improvement/improvement_archive_service.py` — `ImprovementArchiveService`: builds the ZIP bytes and renders `README.md` / transcript markdown
- `backend/app/services/improvement/improvement_download_service.py` — `archive_response()`: the shared audited download chokepoint used by both the web and CLI archive routes

**Commands:**
- `backend/app/services/agents/commands/session_improve_command.py` — `SessionImproveCommandHandler` (`/session-improve`); registered in `backend/app/services/agents/commands/__init__.py`
- `backend/app/services/agents/command_service.py` — guest/webapp-share availability rule for `/session-improve` in `list_for_session`

**Events / Audit:**
- `backend/app/models/events/event.py` — `EventType.IMPROVEMENT_REQUEST_CREATED` / `IMPROVEMENT_REQUEST_UPDATED`
- `backend/app/models/events/security_event.py` — `IMPROVEMENT_ARCHIVE_DOWNLOADED`

**Guide (shipped into the CLI context package):**
- `backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/guides/handling-improvement-requests.md`
- `backend/app/services/cli/context_package_service.py` — `guides/` row + a dedicated paragraph in the generated `context/README.md` index pointing at the guide

**Migration:**
- `backend/app/alembic/versions/227785421f7a_add_agent_improvement_request_table.py`

### Frontend

**Session-side (requester):**
- `frontend/src/components/Sessions/ImproveAgentMenuItem.tsx` — dropdown item; owns its own dialog state, mirroring `EditSession.tsx`
- `frontend/src/components/Sessions/ImproveAgentModal.tsx` — the consent modal (pre-flight query, header disclosure, memory opt-out, comment form, submit) plus the non-exported `SharingDetailsDialog` it opens
- `frontend/src/routes/_layout/session/$sessionId.tsx` — wires `ImproveAgentMenuItem` into the session page's `⋮` dropdown, above `EditSession`

**Agent-side (recipient):**
- `frontend/src/components/Agents/ImprovementRequestsCard.tsx` — Configuration-tab card (list, status filter, live invalidation)
- `frontend/src/components/Agents/ImprovementRequestDetailModal.tsx` — detail view: context summary, status/resolution-note edit, archive download, delete
- `frontend/src/components/Agents/AgentConfigTab.tsx` — renders `ImprovementRequestsCard agentId={agent.id} hideWhenEmpty={readOnly}`; not gated on `showOperationalSettings` or the developer role

**Shared:**
- `frontend/src/utils/improvementRequests.ts` — status label/badge-colour map (`IMPROVEMENT_STATUSES`, `getImprovementStatusMeta`) and `improvementShortId`
- `frontend/src/utils.ts` — `downloadAuthenticatedFile` / `saveBlobAs` (shared authenticated binary-download helper, also used by `Chat/AttachmentBlock.tsx` and `Chat/FileBadge.tsx`)

## Database Schema

### Table: `agent_improvement_request`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK | |
| `session_id` | UUID, nullable | FK `session.id`, `ON DELETE SET NULL` | provenance backlink only — snapshot outlives the session |
| `source_agent_id` | UUID, nullable | FK `agent.id`, `ON DELETE SET NULL` | the consumer-side install the session ran on |
| `target_agent_id` | UUID | FK `agent.id`, `ON DELETE CASCADE`, NOT NULL | the receiving agent — publisher install, or the agent itself |
| `bundle_uuid` | UUID, nullable | FK `agent_bundle.id`, `ON DELETE SET NULL` | set when the source install came from a bundle |
| `requester_user_id` | UUID | FK `user.id`, `ON DELETE CASCADE`, NOT NULL | account deletion cascades — withdraws the shared data |
| `owner_user_id` | UUID | FK `user.id`, `ON DELETE CASCADE`, NOT NULL | denormalised recipient; drives cheap listing + authz |
| `comment` | TEXT, nullable | ≤ 4000 chars, API-enforced | requester's description |
| `status` | VARCHAR(16) | NOT NULL, default `'new'` | plain VARCHAR, no Postgres enum (like `Session.status`) |
| `resolution_note` | TEXT, nullable | ≤ 2000 chars, API-enforced | owner's closing note, visible to the requester |
| `source` | VARCHAR(16) | NOT NULL, default `'web_ui'` | `web_ui` \| `command` |
| `snapshot` | JSON | NOT NULL, default `'{}'::json` | frozen transcript |
| `context` | JSON | NOT NULL, default `'{}'::json` | frozen runtime context |
| `snapshot_message_count` | INT | NOT NULL, default `0` | cheap list-projection field |
| `snapshot_truncated` | BOOL | NOT NULL, default `false` | true when the 2 MB cap dropped messages |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | |
| `status_changed_at` | TIMESTAMPTZ, nullable | | stamped on every status transition |

**Indexes:**

| Index | Columns | Purpose |
|---|---|---|
| `ix_air_target_status` | `(target_agent_id, status)` | the Configuration-tab card query |
| `ix_air_owner_created` | `(owner_user_id, created_at DESC)` | the CLI cross-agent list |
| `ix_air_requester` | `(requester_user_id)` | "my submitted requests" |
| `ix_air_session` | `(session_id)` | per-session rate-limit check |

Migration `227785421f7a` is additive only — no existing table is touched, no backfill. Autogenerate also reported pre-existing local drift unrelated to this feature (`session.channel_*`, `app_agent_route.channels`, `cli_device_login_request` timestamp types); that drift was left out of this migration.

### `snapshot` JSON shape (schema_version 1)

```jsonc
{
  "schema_version": 1,
  "captured_at": "…", "truncated": false, "omitted_message_count": 0,
  "session": { "id", "title", "mode", "status", "result_state", "result_summary",
               "integration_type", "created_at", "last_message_at", "total_message_count" },
  "messages": [
    { "sequence_number", "role", "content", "timestamp", "status",
      "is_command", "command_name",
      "attachments": [{ "filename", "mime_type", "size" }],
      "tool_digest": [{ "seq", "type", "tool_name", "brief" }] }
  ]
}
```

`tool_digest` types kept: `tool_use`, `tool_result`, `thinking`, `error`, plus a synthetic `omitted` marker entry when the 200-entry cap dropped older events. `role != "user"` messages get a digest; user messages never do (there is nothing in `streaming_events` for a user turn).

### `context` JSON shape (schema_version 3)

Eight independently-guarded blocks — `agent`, `environment`, `sdk`, `plugins`, `prompts`, `memory`, `recipient`, `platform` — each built by its own best-effort helper in `SessionSnapshotService`. A failed lookup (missing environment, unreadable plugin manifest, deleted bundle revision) yields `null` / `[]` for that block and never aborts the capture. `sdk.effective_model` is resolved by calling the existing `evaluate_environment` model-health resolver rather than re-implementing the override → credential-default → catalog-tier precedence. `context.platform.scrubbed_hits` records the secret-scrubber's replacement count across both the snapshot and the context (never the values), added post-hoc by `ImprovementRequestService.create_from_session`.

`schema_version 1` rows predate `prompts` / `memory` and simply lack both keys — every reader (archive renderer, detail modal) treats an absent `prompts` block as "captured before this existed", not as "no prompts".

`schema_version 3` changed what the blocks *claim*, not which blocks exist: the prompt-divergence rollup narrowed to the published prompt documents, and the `agent` block's "latest" pointer was renamed to say that it means "latest **published**". Rows captured at version 2 are still rendered — the archive and the detail modal read the new keys and fall back to the old names, since the facts those rows captured were correct under their own names.

#### `agent` block

| Key | Meaning |
|---|---|
| `installed_revision_number` / `installed_version` | The revision this install was materialised from. `version` is `null` for every git-origin revision — *unversioned*, not unknown, and rendered that way. |
| `installed_revision_origin` | `publish` or `git`. |
| `latest_published_revision_number` / `latest_published_version` | The bundle's `latest_revision_id` pointer, which **only the publish path advances** (was `latest_revision_number` / `latest_version` at schema 2). |
| `head_revision_number` | Highest revision number on the bundle whatever its origin. |

Revision numbers are allocated across publish **and** git revisions, so an install legitimately sits *above* the latest published revision with nothing pending. The README says so in words when it happens (`_revision_track_note`) — left bare, "installed 9 / latest published 7 / update pending: no" reads as a regression and as a contradiction.

#### `prompts` block

```jsonc
"prompts": {
  "schema_version": 3,
  "baseline": "installed_revision",   // or "none"
  "baseline_version": "1.3",
  "diverged": true,                    // null when baseline == "none"
  "diverged_fields": ["workflow"],     // published prompts only
  "workflow":       { "role": "published_prompt",
                      "chars": 4210, "sha256": "…", "updated_at": "2026-08-12T…",
                      "diverged_from_installed_revision": true,
                      "divergence_reason": null,
                      "truncated": false, "text": "…" },
  "entrypoint":     { … }, "refiner": { … },
  "router_trigger": { "role": "routing_metadata",
                      "diverged_from_installed_revision": null,
                      "divergence_reason": "platform_managed_no_baseline", … },
  "sdk_tools":      ["Bash", "Read", …],
  "allowed_tools":  ["Read", …],       // null = no auto-approval list configured
  "example_prompts": ["…"]
}
```

- Field keys map to `Agent` / `AgentBundleRevision` columns through `_PROMPT_FIELDS` — the same attribute name exists on both models, which is what makes the divergence comparison a one-liner.
- `diverged_from_installed_revision` is **tri-state**: `true` / `false` when there is an installed revision to diff against, `null` when there is not. Never `false`-by-default — that would assert a match that was never checked.
- Comparison normalises leading/trailing whitespace only (`_normalise`); everything else is a real difference.
- `sha256` is computed **pre-scrub**, so it identifies the text as the agent ran it. A `text` containing `***REDACTED***` will not re-hash to it; `prompts/README.md` in the archive states this.
- `text` is tail-truncated at `MAX_PROMPT_TEXT_CHARS = 40_000` with `truncated: true`, and is `null` when the field was never set.
- `role` splits the four fields in two. `published_prompt` (workflow / entrypoint / refiner) is what a publisher writes and ships; `routing_metadata` is `router_trigger`, which says *when to route to* the agent rather than how it behaves. Only `published_prompt` fields feed the `diverged` rollup and `diverged_fields`.
- **`router_trigger` is never reported as an edit against an absent baseline.** The platform writes that column by itself (`scripts/backfill_router_trigger_prompts` AI-generates one wherever a foreign install has no auto-managed route) and `PATCH /agents/{id}/router-trigger-prompt` is open to `agent-user` accounts by design. Publishers rarely set it, so a naive comparison fires on close to every consumer install, for text no person wrote — and a flag that is usually wrong gets ignored, taking the genuine workflow-prompt signal with it. When the baseline field is empty the flag stays `null` with `divergence_reason: "platform_managed_no_baseline"`, rendered by the archive as *not compared (routing metadata)* — deliberately distinct from *unknown*, which means "no baseline existed at all".
- `divergence_reason` vocabulary: `null` (a comparison was made), `no_baseline` (no installed revision), `platform_managed_no_baseline`.
- `allowed_tools` is `null` when the agent has no auto-approval list at all and `[]` when it has an empty one. Both prompt on every tool use, and neither restricts which tools exist — but as a bare empty list they read as opposite answers to "why did it not use that tool", so the archive spells out which one it is.
- `router_trigger` has no `*_updated_at` clock on `Agent` (only the three bidirectional prompts do), so its `updated_at` is always `null`. With the flag no longer asserting an edit for that field, nothing depends on the missing clock.

#### `memory` block

```jsonc
"memory": {
  "schema_version": 2,
  "available": true, "unavailable_reason": null,
  "captured_at": "…", "file_count": 2, "total_chars": 3120, "truncated": false,
  "files": [{ "filename": "MEMORY.md", "chars": 1200, "sha256": "…",
              "truncated": false, "text": "…" }]
}
```

- The **only live container read in the feature**, and it happens before the row is written. `capture_memory` runs one `AgentEnvConnector.exec_command` against the source install's environment, piping a small reader script in over **stdin** (`command="python3 -"`) so nothing is embedded in a shell string.
- The reader mirrors `prompt_generator._load_personal_memory`: `/app/workspace/app-data/memory/*.md`, sorted case-insensitively by filename — the same order the runtime injects them in — and emits JSON on stdout so the backend never parses a delimiter out of arbitrary file content.
- It **never wakes a stopped container**: `environment.status != "running"` short-circuits to `env_not_running`. Timeout is `MEMORY_READ_TIMEOUT = 15s`, because this runs inline on the submit request.
- Caps: `MAX_MEMORY_FILES = 20`, `MAX_MEMORY_TOTAL_CHARS = 20_000` — the total deliberately mirrors `PERSONAL_MEMORY_MAX_CHARS` in `app_core_base/core/server/prompt_generator.py`, so the capture can never show more than the runtime could inject.
- `unavailable_reason` vocabulary: `declined_by_requester`, `no_environment`, `env_not_running`, `read_failed`, `empty`.
- Filenames come from inside the requester's container and are therefore **untrusted**. `_safe_member_name` reduces each to a bare basename (path separators stripped, non-`[A-Za-z0-9._-]` replaced, leading dots removed, 80-char cap, positional fallback) at capture time, so nothing can escape `memory/` when the archive is extracted. `_memory_members` additionally disambiguates post-sanitising collisions rather than overwriting.

## API Endpoints

### `backend/app/api/routes/improvement_requests.py` (tag `improvement-requests`)

| Method | Path | Who | Response |
|---|---|---|---|
| GET | `/sessions/{session_id}/improvement-context` | requester | `ImprovementContextPublic` |
| POST | `/improvement-requests` | requester | `ImprovementRequestPublic` (201) |
| GET | `/improvement-requests/mine` | requester | `ImprovementRequestsPublic` |
| GET | `/agents/{agent_id}/improvement-requests?status=&skip=&limit=` | recipient | `ImprovementRequestsPublic` |
| GET | `/improvement-requests/{request_id}` | either | `ImprovementRequestDetailPublic` |
| GET | `/improvement-requests/{request_id}/archive` | either (audited if cross-user) | raw `Response`, `application/zip` |
| PATCH | `/improvement-requests/{request_id}` | recipient | `ImprovementRequestDetailPublic` |
| DELETE | `/improvement-requests/{request_id}` | recipient | `Message` |

`ImprovementRequestPublic` carries `session_id`, `is_bundle_install` and `installed_revision_number` alongside `bundle_id` / `installed_version`. Both were added because the listing surfaces could not otherwise answer questions the row already knew: two captures of one conversation are indistinguishable without the session id (titles are neither unique nor stable), and `installed_version: null` is routine on a real bundle install — every git-origin revision is unversioned — so a consumer keying "is this a bundle?" off the version label mislabels it as standalone.

`/improvement-requests/mine` is declared **before** `/improvement-requests/{request_id}` so FastAPI does not try to parse `mine` as a UUID. The archive route has **no `response_model`** — mirrors the `/account/api-proxy` raw-passthrough pattern; the generated TS client therefore types it as a blob, and the frontend fetches it with `downloadAuthenticatedFile` rather than the generated service method.

### `backend/app/api/routes/cli.py` — `/account/improvement-requests*`

Authenticated by `AccountCLIContextDep`; every route delegates straight into `ImprovementRequestService` / `archive_response`, so ownership rules cannot drift between the web and CLI transports.

| Method | Path | Notes |
|---|---|---|
| GET | `/account/improvement-requests?status=&agent_id=&limit=` | cross-agent list of everything the account user *receives* |
| GET | `/account/improvement-requests/{id}` | detail incl. `context` |
| GET | `/account/improvement-requests/{id}/archive` | binary ZIP — dedicated route because the JSON-only `/account/api-proxy` cannot carry a binary body |
| PATCH | `/account/improvement-requests/{id}` | `{status, resolution_note}` |

The `improvement-requests` prefix is deliberately **not** on the `/account/api-proxy` denylist — `cinna api` can also reach the JSON endpoints (`GET`/`PATCH`); the dedicated routes exist for ergonomics and, for the archive, out of necessity.

## Service Layer

### `ImprovementRequestService` (`improvement_request_service.py`)

- `resolve_target(db, source_agent) -> TargetResolution` — bundle-consumer → publisher install; publisher-unavailable → self with `fallback_reason="publisher_unavailable"`; everything else → self. See business doc for the full decision tree.
- `_evaluate_eligibility` / `_check_rate_limits` — the shared eligibility gate (§ business doc), used identically by `create_from_session` (raises `ImprovementRequestDenied`) and `build_context_preview` (reports `eligible=False` + `reason`).
- `create_from_session(db, session, requester, comment, source, include_memory=True) -> AgentImprovementRequest` — runs the gate, resolves the target, calls `SessionSnapshotService.capture` + `capture_context` + `await capture_memory`, scrubs **both** the snapshot and the context via `secret_scrubber.scrub` (summing the hit counts into `context.platform.scrubbed_hits`), writes the row, emits `IMPROVEMENT_REQUEST_CREATED` to the recipient's user room. `include_memory` is the requester's opt-out; `False` reads nothing from the container at all.
- `build_context_preview(db, session, user) -> ImprovementContextPublic` — the modal's pre-flight; writes nothing, runs the same gate + resolution.
- `list_for_agent` / `list_for_owner` / `list_for_requester` — all funnel through `_list`, which batches agent-name and requester-identity lookups with two `IN` queries via `project_many` (no per-row `db.get`).
- `get_authorized(db, request_id, user) -> (row, role)` — 404 for anyone not the recipient or requester (`role ∈ {"owner", "requester"}`).
- `update_status` / `delete` — recipient-only, enforced by `_assert_owner` (requester who is party to the row gets 403; a stranger gets 404, since `get_authorized` already ran first).
- `build_archive(db, request) -> (bytes, filename)` — assembles the requester/target projections and calls `ImprovementArchiveService.build`; shared by the web and CLI archive routes.
- `_collect_secrets(db, source_agent) -> set[str]` — the source install's linked `Credential` rows (via `CredentialsService.get_agent_credentials_with_data`, filtered by `CredentialsService.SENSITIVE_FIELDS`) plus its environment's AI credential API keys. Failures log only the exception *type*, never the exception itself (a Pydantic `ValidationError` would otherwise render the offending value inline).

### `SessionSnapshotService` (`session_snapshot_service.py`)

- `capture(db, session) -> (snapshot, truncated, message_count)` — pages messages **newest-first** with keyset pagination (`sequence_number <` cursor, not `OFFSET`, so a concurrently-appended message cannot shift the window), stopping the moment the running serialized size would exceed the 2 MB budget. This bounds peak memory to the surviving budget plus one page rather than the whole session, since `message_metadata.streaming_events` — discarded by the digest — is the largest field in the DB.
- `_tool_digest(streaming_events)` — scans the raw events **newest-first**, keeps at most 200, reverses back to chronological order, and prepends an `omitted` marker entry when the cap dropped older events.
- `capture_context(db, session, source_agent, resolution) -> dict` — builds the DB-backed context blocks; every sub-helper (`_agent_block`, `_environment_block`, `_sdk_block`, `_plugins_block`, `_prompts_block`, `_recipient_block`) independently try/excepts its own lookup. Synchronous.
- `_prompts_block(db, source_agent) -> dict` — the four prompt documents plus `agent_sdk_config` tool lists, with per-field divergence against `AgentBundleRevision` (see the `prompts` block above). Field roles come from `_PROMPT_FIELDS`, which is where the published-vs-routing split lives. Loses only the baseline if the revision lookup fails.
- `capture_memory(db, session, source_agent, include=True) -> dict` — **async**, and deliberately separate from `capture_context`: the memory area is the one part of the run context that never reaches the database, so it needs a container read. Never raises — every failure path returns `_memory_unavailable(reason)`.
- `_memory_block(raw_files)` — shapes, sanitises and caps the reader's output; `_safe_member_name` is the untrusted-filename chokepoint.

### `secret_scrubber` (`secret_scrubber.py`)

Pure function, no DB access — `scrub(payload: dict, secrets: set[str]) -> (dict, hits: int)`. Called twice per submission: once on the snapshot, once on the context. Walks the payload recursively and rewrites strings under `SCRUBBED_KEYS = {content, brief, result_summary, title, text}` only (`text` is the prompt/memory document field, and is why the context block is scrubbed at all); structural fields (ids, names, timestamps, mime types) are left untouched so a false positive can never corrupt the archive's metadata. Secrets shorter than `MIN_SECRET_LENGTH = 8` are dropped from the set before matching. Follows the `assert_url_allowed` / `assert_api_proxy_allowed` chokepoint precedent — exhaustively unit-testable in isolation from the DB.

### `ImprovementArchiveService` (`improvement_archive_service.py`)

`build(request, requester_projection, target_projection) -> bytes` assembles an in-memory `zipfile.ZipFile` (fixed `ZipInfo` timestamp `1980-01-01` so the same row always produces byte-identical output). Archive `schema_version 2`:

```
improvement-<short-id>.zip
├── README.md
├── metadata.json
├── context.json
├── prompts/                       (only for schema_version ≥ 2 rows with prompt text)
│   ├── README.md                  divergence table + tool configuration
│   ├── WORKFLOW_PROMPT.md
│   ├── ENTRYPOINT_PROMPT.md
│   ├── REFINER_PROMPT.md
│   └── ROUTER_TRIGGER_PROMPT.md
├── memory/                        (only when memory was actually captured)
│   ├── README.md
│   └── <the install's app-data/memory/*.md>
└── session/
    ├── messages.md
    └── messages.json
```

Prompt members are named after the workspace documents they mirror (`docs/WORKFLOW_PROMPT.md` and friends) so a publisher can diff a member straight against the file in their own install. A field with no text is **skipped** rather than written empty — an empty `REFINER_PROMPT.md` would read as "the consumer blanked it" when the truth is "this agent never had one" — and `prompts/README.md` marks it `(not set)` in the table.

`render_readme` renders, in order: what was reported, who/when, which agent (bundle id / installed vs. latest **published** revision, each labelled by `_revision_label` so an unversioned git revision reads as `revision 9 (unversioned) · from git` rather than a bare em-dash, plus `_revision_track_note` when the install sits above the published pointer / update-pending when applicable), a runtime-context markdown table, a **Prompts and memory** section (`_readme_prompts_section` — names the diverged documents, or says there was no baseline; states the memory file count or the reason it is absent, via `_MEMORY_REASON_COPY`), what is and is not in the archive (with the truncation notice when `snapshot_truncated`), and how to act — pointing at the shipped guide and stating the bundle-publisher golden rule (fix the publisher install, publish a new version, never edit a consumer's install) whenever the request came from a consumer install. No filesystem writes anywhere — deliberately, to avoid a new write path needing a docker-compose volume mount.

### `improvement_download_service.archive_response` (`improvement_download_service.py`)

The single chokepoint both the web and CLI archive routes call: builds the archive via `ImprovementRequestService.build_archive`, writes the `IMPROVEMENT_ARCHIVE_DOWNLOADED` `SecurityEvent` **only** when `owner_user_id != requester_user_id`, and returns the `Response` with the `Content-Disposition` header. The audit write is fail-open (rolls back and logs on failure) so a database hiccup never denies a legitimate download — matching the `environment_console_service` audit precedent.

## `/session-improve` Command

`SessionImproveCommandHandler` (`session_improve_command.py`):

```python
streams = False
include_in_llm_context = False   # meta-command; the LLM must not see it
requires_running_environment = False   # never wakes a container
name = "/session-improve"
```

`requires_running_environment = False` still holds with memory capture: the read is opportunistic (a stopped container records `env_not_running`), because submitting a report must not start billable compute.

`_parse_args(args) -> (comment, include_memory)` strips the `--no-memory` literal from anywhere in the argument string and treats the remainder as the comment — matched as a literal rather than run through an argument parser, so a comment can never be silently eaten by argument parsing.

`execute` loads the session and the requester `User` from a fresh DB session, calls `ImprovementRequestService.create_from_session(..., source="command", include_memory=…)`, and returns a markdown confirmation built by `_confirmation(request.context)` that names the recipient and, via `_included_line`, what configuration rode along — the prompts always, the memory files only when `context.memory.available` is true (saying otherwise would overstate what was shared) — or states plainly that nothing left the account when `context.recipient.is_shared_externally` is false. A denial maps to `CommandResult(content=e.message, is_error=True)`.

**Autocomplete availability:** `CommandService.list_for_session` marks `/session-improve` `is_available=False` when `chat_session.guest_share_id is not None or chat_session.webapp_share_id is not None` — the same shape as the existing `/rebuild-env` availability check.

## Events & Audit

- `EventType.IMPROVEMENT_REQUEST_CREATED` / `IMPROVEMENT_REQUEST_UPDATED` — emitted by `ImprovementRequestService._emit` to the **recipient's** (`owner_user_id`) user room. Meta carries `request_id`, `target_agent_id`, `source_agent_id`, `bundle_uuid`, `status`. Emission failures are logged and swallowed — the row is already committed.
- `SecurityEvent.IMPROVEMENT_ARCHIVE_DOWNLOADED` — written by `improvement_download_service._audit` only for cross-user downloads; payload carries request id, target agent id, bundle id/uuid, requester user id, acting user id, source IP — never conversation content.

## Frontend Component Map

- **`ImproveAgentModal.tsx`** — `useQuery(["improvementContext", sessionId], …)`, `enabled: open`, `staleTime: 0`. Renders one of: loading skeleton, error, ineligible (reason copy from a local `REASON_COPY` map keyed on the backend's machine-readable reason string), or the eligible form. **Header.** When the pre-flight resolves eligible, the `DialogDescription` is rendered `asChild` around a filled, border-less `role`-free info block (`bg-muted/60`, leading `Info` icon) carrying *both* orienting sentences at one `text-sm` size: `renderRecipientLine()` — bundle publisher / agent owner / own-agent note-to-self — and the capture-size line (`N messages will be captured…`, plus the already-submitted count). They were previously two paragraphs at `text-sm` and `text-xs` in two different places, which read as unrelated notes at jumping sizes. `asChild` is what keeps the visible block the dialog's real `aria-describedby` target instead of a decorative div beside a hidden description. Agent and recipient names render as `Badge variant="secondary"` (a `<span>`, so it is valid inside the `<p>`). While the query is in flight or the session is ineligible the block collapses to a plain description that promises no recipient at all.

  **Body.** Deliberately short: an amber callout **only** when `is_shared_externally` (bundle coordinates + irreversibility — the recipient's name lives in the header, so repeating it here was the redundancy), the `includeMemory` checkbox, and the comment box. The self-targeted case renders no callout at all: `rounded-md border bg-muted/50` directly above a `Textarea` reads as a second input rather than a notice. A single-line `includeMemory` checkbox — *"Include MEMORY files of this agent"* (default `true`, mapped to `ImprovementRequestCreate.include_memory`) — carries the personal-memory decision; it is the only captured block that is the requester's own content rather than agent configuration, so it stays on the form even though the rest of the itemisation moved off it, with the explanation of what those files are carried by the mirroring row in `SharingDetailsDialog`. The `<form>` sets `grid gap-4`: it is a single grid child of `DialogContent`, so the dialog's own `gap-4` never lands between header, body and footer, and without it the checkbox sits flush against the callout above and the textarea below. `react-hook-form` + `zod` validates the optional `comment` (≤ 4000 chars) and the boolean, to keep the max-length message consistent with the rest of the app. On success: toast, form reset, modal close, invalidates `["improvementRequests"]` and `["improvementContext", sessionId]`.
- **`SharingDetailsDialog`** (same file, not exported) — the full disclosure, opened by a left-aligned ghost **"Sharing details"** button in the footer. Renders the two-column **Included / Not included** list — Included names the `tool_digest` (commands run, files touched, results), attachment names/types/sizes, the session title/outcome, agent/environment/model settings, the agent's prompt documents and tool configuration, and the requester's own name and email; Not included is narrowed to attachment contents, the agent's scripts and knowledge base, container logs, and other sessions — plus a `role="note"` amber warning that credential masking is best-effort. The memory row is **not** in either static list: it is rendered from the parent's `form.watch("includeMemory")`, so the dialog describes the submission the user is about to make rather than a fixed one. Three mechanics worth keeping: the trigger is `type="button"` (the shared `Button` sets no type, so inside the form it would default to submit and fire the request); the dialog is mounted inside the parent's `DialogContent` but Radix portals it to `body`, so it stacks rather than nesting a form; and `detailsOpen` is cleared both in the mutation's `onSuccess` and in the parent `Dialog`'s `onOpenChange`, or reopening the modal would land straight back on the disclosure. Its footer is a single outline **Close** button.
- **`ImprovementRequestsCard.tsx`** — two queries share one cache key shape: `["improvementRequests", agentId, statusFilter]` for the table and `["improvementRequests", agentId, "new"]` for the header badge/count (free when the filter is already `"new"`). Accepts a `hideWhenEmpty` prop; when true (foreign/read-only installs) a third, unfiltered `limit: 1` existence-probe query (`["improvementRequests", agentId, "any"]`) gates the card to `null` while pending or when the agent has zero requests, so the card does not flash in and out — this makes visible the `resolve_target` fallback case where a bundle's publisher install is unreachable and the request lands on the consumer's own install, which is the only person who can act on it. `useMultiEventSubscription([IMPROVEMENT_REQUEST_CREATED, IMPROVEMENT_REQUEST_UPDATED], invalidate)` where `invalidate` is a `useCallback` closing over `queryClient` only (not `agentId`) since the subscription hook captures its handler once. Table rows are `role="button" tabIndex={0}` with Enter/Space handling for keyboard access.
- **`ImprovementRequestDetailModal.tsx`** — `useQuery(["improvementRequest", requestId], …)`; keeps rendering the last-fetched request via a ref while `requestId` is nulled during the dialog's close animation, to avoid a skeleton flash. `handleDownload` calls `downloadAuthenticatedFile("/api/v1/improvement-requests/{id}/archive", "improvement-<short-id>.zip")` directly (bypassing the generated client, since the route has no `response_model`). Status and resolution note are edited together as one `{requestId, status, note}` draft committed by a single **Save changes** button (rather than two independent controls), tagged with the request id so switching rows falls back to that row's saved values instead of carrying an unsaved draft across. The note's caption reads *"Stored with the request. The person who submitted it can read it through the API and the CLI."* — deliberately not "visible to the requester", since there is no requester-facing UI page for it in v1. Two detail rows summarise the capture: **Prompts edited on the install** renders `context.prompts.diverged` as a tri-state (`null` → *"no baseline to compare against"*, never *"no"*) and names `diverged_fields` when there are any, and **Personal memory** renders either the captured file count or the human copy for `context.memory.unavailable_reason` from a local `MEMORY_REASON_COPY` map. Both rows are omitted entirely for `schema_version 1` rows, which have no `prompts` block — an empty row would read as "no prompts".
- **`ImproveAgentMenuItem.tsx`** — owns its own `isOpen` state, mirroring `EditSession.tsx`; wired into `session/$sessionId.tsx`'s dropdown above `EditSession`.
- **`utils/improvementRequests.ts`** — `IMPROVEMENT_STATUSES` tuple, `getImprovementStatusMeta` / `getImprovementStatusLabel` (badge label + full Tailwind class names — JIT cannot see fragments — with a graceful `Unknown` fallback for a status value the frontend has not been taught, since the backend's `status` is an unconstrained VARCHAR), `improvementShortId` (first 8 chars, matching the archive filename and the CLI's extraction directory).

## Migration

`227785421f7a_add_agent_improvement_request_table.py` — creates `agent_improvement_request` with the four indexes above. Additive only. `status` / `source` are plain `VARCHAR` with server defaults (not Postgres enums), matching `Session.status` / `AgentApiToken.kind` — a new status value needs no migration. `snapshot` / `context` default to `'{}'::json`, mirroring `AgentBundleRevision.manifest`.

## Rollout Notes

- **No agent-env change.** Nothing touches the container image or `/app/core` — no environment rebuild is required for this feature to work on existing environments. The personal-memory capture runs a reader script through the **existing** `/exec` endpoint, so it works against containers built before this feature landed. (A container old enough to predate `prompt_generator`'s memory reader has no `app-data/memory` directory and records `empty`; one old enough to predate `/exec`'s `stdin` field degrades to `read_failed`. Both are honest outcomes, and neither blocks the submission.)
- **No migration for prompts/memory.** They live inside the existing `context` JSON column. `context.schema_version` goes `1 → 2 → 3` and archive `schema_version` goes `1 → 2` (the archive's *member layout* is unchanged at 3 — only what the prose asserts); pre-existing rows keep version 1 and are rendered without the new sections rather than backfilled — a backfill would read the *current* prompts and memory, which is exactly the live-read-through the feature forbids.
- **No new filesystem write path.** Snapshots live in JSONB; the archive is built in memory on every download. No docker-compose volume mount was added.
- **Client regeneration was required** after the backend routes landed (`make gen-client`), generating `ImprovementRequestsService` in `frontend/src/client/`.
- **Context package** — the guide is picked up automatically by `ContextPackageService`'s `knowledge/guides/**` glob; only the generated `context/README.md` index string needed a manual edit (already done in `context_package_service.py`).
