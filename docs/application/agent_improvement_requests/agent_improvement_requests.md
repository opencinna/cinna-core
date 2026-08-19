# Agent Improvement Requests

## Purpose

When a user chatting with an agent hits a scenario the agent handled badly, that knowledge is normally trapped: if the agent is a bundle install, the publisher cannot read the consumer's session (sessions are strictly owner-scoped), so the defect never reaches the person who can fix it.

Agent Improvement Requests add a consent-gated, one-directional data channel: the *session owner* explicitly shares a **frozen snapshot** of one session — plus the tuning-relevant runtime context — with the *agent's owner* (the bundle publisher, or themselves for a standalone agent). The recipient sees the request in a card on the agent's Configuration tab, downloads a self-describing ZIP archive, and — via the `cinna-cli` account workspace — hands the archive to a local coding agent that knows how to turn it into a fix.

Two entry points raise a request: the **Improve Agent** item in a session's `⋮` options menu (opens a consent modal), and the `/session-improve [comment]` slash command. Both go through the same service call, so the eligibility rules, target resolution, and secret scrubbing are identical regardless of which surface the user consented from.

## Core Concepts

- **Improvement Request** — an `AgentImprovementRequest` row: a frozen transcript snapshot, a frozen runtime-context block, a requester comment, a status, and an owner's resolution note. Created once, at consent time; never refreshed.
- **Requester** — the session owner who consents to share. Only they can create a request, and only for a session they own.
- **Recipient / owner** — the user who receives the request: the bundle **publisher** for a consumer install, or the requester **themselves** for a standalone agent or their own publisher install.
- **Snapshot** — the frozen transcript: every message (`user` / `agent` / `system`) ordered by `sequence_number`, a compact `tool_digest` per agent message (never the raw `streaming_events`), and attachment **descriptors** (filename / mime type / size — never file bytes).
- **Context** — the frozen runtime block: agent/bundle identity and version, environment (name, image tag, staleness, critical state), SDK engine and **effective model** (resolved with the same override → credential-default → catalog-tier chain the environment uses), the plugin manifest, and recipient/fallback info.
- **No-live-read-through invariant** — after the row is written, nothing in this feature ever reads the source `Session` again. The archive is a pure function of the stored `snapshot` / `context`. Continuing or deleting the conversation does not change what the recipient sees, and there is **no withdrawal** in v1: consent is the write, and it is final.
- **Improvement archive** — a self-describing ZIP (`README.md`, `metadata.json`, `context.json`, `session/messages.md`, `session/messages.json`) built in memory, on demand, from the stored row. Never cached, no filesystem writes.
- **Status workflow** — `new` → `in_progress` → `completed` / `declined`, set by the recipient only. The `resolution_note` on a status change is stored on the row and readable by the requester through the API and the CLI (`GET /improvement-requests/mine` / `cinna improve show`) — there is no dedicated "my requests" UI page in v1.
- **Secret scrubbing** — a **best-effort**, not absolute, redaction pass. Only the source install's own linked credential values (its `Credential` rows filtered through the shared sensitive-field map, plus its environment's AI credential API keys) are masked out of the transcript's free-text fields before the row is written. A secret the user typed themselves — a password, a token pasted from somewhere else, anything not already stored as one of this install's credentials — is **not** recognized and is shared exactly as written. See [Business Rules § Secret scrubbing](#secret-scrubbing) for the exact scope.

## User Stories / Flows

### 1. Raising a request from the session menu

1. In a session, the user opens the `⋮` menu and clicks **Improve Agent**.
2. The **Improve Agent** modal opens and fetches a pre-flight preview (`GET /sessions/{id}/improvement-context`) — the *same* eligibility check and target resolution the submission will run, so the modal's copy can never disagree with what clicking submit actually does.
3. If the session is not eligible (guest/webapp share, empty, agent deleted, not the owner), the modal shows the reason and hides the form.
4. A single **info block** at the top — one type size, led by an info icon — answers both orienting questions at once. First, where it goes, named from the resolved recipient rather than hedged, since the outcomes are different actions: *"Goes to `Jane P.`, who publishes the bundle this agent was installed from"*, *"…who owns this agent"*, or — for a self-owned agent — *"Kept on your own agent `Friendly Chap` as a note for later improvements. Nothing leaves your account."* Agent and recipient names render as badges. Second, how much is captured: *"4 messages will be captured as they are right now. Continuing the conversation afterwards won't change what is shared,"* plus the count of requests already submitted for this session. While the pre-flight is still running, or when the session is ineligible, the block collapses to a line that promises no recipient at all.

   On top of that, an external recipient gets an amber callout carrying what the info block cannot: the bundle id, the installed version, and **"This cannot be undone."** A self-targeted request gets no callout — the info block already says it, and a second bordered panel above the comment box read as an input rather than a notice.
5. A **Sharing details** button in the footer opens a sub-dialog with the full **Included / Not included** list. Included: every message and what the agent did in between (the `tool_digest` — commands run, files touched, results, thinking, errors), the names/types/sizes of attached files (not their contents), the session's title and outcome, agent/environment/model settings and the installed bundle version, **the agent's prompt documents and tool configuration**, and the requester's own name and email. Not included: the contents of any attached file, the agent's scripts and knowledge base, container logs, and any of the requester's other sessions. The Not-included list deliberately claims nothing about `app-data/memory`; the dialog instead renders a live row that follows the form's checkbox, so it always describes the submission about to be made. A separate amber warning in the same dialog notes that credential masking is best-effort and covers only credentials saved on this agent — anything the user typed into the conversation themselves is shared exactly as written.

   The itemisation sits behind a button rather than inline so the common case — a user who already knows what a bug report contains — is a recipient line, a checkbox and a text box. What stays on the form is what a user cannot be assumed to know: who receives it, how much is captured, and that it cannot be undone.
6. A checkbox — **checked by default** — reads *"Include MEMORY files of this agent"* (`app-data/memory`). It is the only captured block that is the requester's own content rather than agent configuration, so it gets its own decision on the form; what those files are is spelled out by a live row in the Sharing details dialog, which follows the checkbox. Unchecking it means nothing is read from the container at all.
7. The user optionally writes a comment (≤ 4000 chars) describing what went wrong, then submits.
8. On success, the request is created with `status="new"`, `source="web_ui"`, and the recipient's Configuration-tab card badge updates live via a WebSocket event.

### 2. Raising a request with `/session-improve`

1. The user types `/session-improve <comment>` in the chat input. The trimmed argument text becomes the comment; the command has no confirmation UI of its own. Adding `--no-memory` anywhere in the arguments leaves the personal memory area out — it is stripped from the text before the rest becomes the comment.
2. The command runs the exact same eligibility gate, target resolution, capture, and scrub as the modal path (`source="command"`).
3. On success, the command's own reply is the disclosure: it names the recipient, the bundle id, and the installed version (e.g. *"…shared with **Jane P.**, publisher of `io.opencinna.cinna.a1b2c3d4` (v1.3)."*), then says what configuration rode along — the prompts always, and the personal memory files only when they were actually captured. It states plainly that nothing left the account when the target is the user's own agent.
4. The command is a sync, non-LLM-context, no-environment operation — see [Agent Commands](../../agents/agent_commands/agent_commands.md#session-improve) — and its output is never forwarded to the LLM as prior-turn context, since surfacing the report inside the very session being reported on would distort it.
5. `/session-improve` is marked **unavailable** in the autocomplete popup on guest-share and webapp-share sessions, matching rule 2 of the eligibility gate below.

### 3. Recipient reviews and downloads

1. The agent owner opens the agent's **Configuration tab**. The **Improvement Requests** card lists requests received on that agent, defaulting to the `New` filter with a live count. On a foreign (read-only, non-publisher) install the card renders only once a request actually exists — the fallback path where a bundle's publisher install is unreachable and the request lands on the consumer's own install instead (see Recipient resolution below) would otherwise be invisible to the only person who can act on it.
2. Each row shows requester, comment (truncated), the install's bundle version at capture, date, and a status badge (`new` violet, `in_progress` blue, `completed` green, `declined` muted). The card's empty state teaches both entry points (`⋮` menu and `/session-improve`).
3. Clicking a row opens the detail modal: the comment (rendered as plain text, never markdown), a frozen-context summary, a combined status `Select` + `resolution_note` textarea saved together by one **Save changes** button, a **Download session archive** button, and a destructive **Delete** with confirmation. The note's caption is deliberately precise about where it goes — *"Stored with the request. The person who submitted it can read it through the API and the CLI."* — since there is no requester-facing UI page in v1.
4. Downloading the archive is audited: every cross-user download (recipient ≠ requester) writes an `IMPROVEMENT_ARCHIVE_DOWNLOADED` security event; a request the user raised on their own agent is not audited on download.
5. Closing the loop sets `status="completed"` or `"declined"` with an optional note — the only reply channel this feature offers back to the requester.

### 4. Recipient acts via the account CLI

1. From the local `cinna-cli` account workspace, the recipient (or the coding agent acting on their behalf) runs `cinna improve list --status new`, `cinna improve show <id>`, and `cinna improve download <id>` to extract the archive into `improvements/<short-id>/`.
2. The bundled guide `context/guides/handling-improvement-requests.md` (shipped in every account workspace's `context/` tree) walks the agent through establishing ownership (standalone agent vs. publisher install vs. a foreign install a fallback landed on), deciding how much to change without asking, fixing it, and closing the loop with `cinna improve status <id> completed --note "…"`.
3. See [Account CLI Workspace](../cinna_cli_integration/account_cli_workspace.md#improvement-requests-cinna-improve) for the CLI verb tables.

### 5. My submitted requests (requester side)

The requester's own submissions are listable via `GET /improvement-requests/mine`, including the recipient's `resolution_note` — it is the requester's own data, so nothing is withheld from their own projection.

## Business Rules

### Recipient resolution

`ImprovementRequestService.resolve_target` decides who receives a request, and is run identically by the pre-flight preview and the actual submission so the two can never disagree:

1. **Bundle consumer install** (`bundle_uuid` set and not the publisher install) → the bundle's **publisher install** (`Agent.bundle_uuid == bundle.id AND is_publisher_install == True`); the recipient is the publisher install's `owner_id`.
2. **Publisher install deleted, or an ownerless git-imported bundle** → falls back to **self**: target = the source agent, recipient = the source agent's own owner, `context.recipient.fallback_reason = "publisher_unavailable"`. This is safe by construction — falling back to self can only ever *narrow* who can read the data, never widen it.
3. **Everything else** (standalone agent, or the publisher's own working install) → **self**.

### Eligibility gate (submission)

All must hold, else the request is denied with a machine-readable reason (`not_owner`, `not_eligible`, `empty_session`, `agent_missing`, `rate_limited`):

1. `session.user_id == requester.id` — only the session's own owner can share it (403).
2. `session.guest_share_id IS NULL AND session.webapp_share_id IS NULL` — guest and webapp-share sessions have no identifiable consenting account.
3. The session has ≥ 1 message.
4. `session.agent_id` resolves to an existing `Agent`.
5. Rate limits: ≤ **5** requests per session, ≤ **20** per requester per rolling 24 hours, else 429.

### What is captured, and what is not

- **Messages** — content capped at **50,000 chars per message** (tail-truncated); all three roles included.
- **`tool_digest`** — a compact per-agent-message digest derived from `streaming_events` (never copied verbatim): at most **200 entries**, briefs capped at **500 chars**, kept **newest-first** — a turn that fails after hundreds of tool calls ships the calls around the failure, not the setup work that preceded it. Dropped older entries are recorded by a leading `omitted` marker entry.
- **Total snapshot cap** — **2 MB** serialized. When exceeded, the **oldest** messages are dropped first (defects cluster at the end of a conversation); the row records `snapshot_truncated=true` and `omitted_message_count`.
- **Attachments** — descriptors only (filename, mime type, size). File bytes never enter the snapshot or the archive.
- **Prompt documents** — the install's `workflow`, `entrypoint`, `refiner` and `router_trigger` prompts in full (capped at **40,000 chars** each), plus `sdk_tools` / `allowed_tools` and the configured example prompts. Always captured; there is no opt-out. See *Why prompts and memory are captured* below.
- **Personal memory** — the install's `app-data/memory/*.md` files (at most **20 files**, **20,000 chars** total — the same cap the runtime applies when it injects them). Captured **by default with a per-submission opt-out**: the web modal exposes a checkbox, `/session-improve --no-memory` is the chat equivalent.
- **Never captured** — container logs (per-environment, would leak the requester's other sessions), uploaded file contents, and everything else in the workspace: scripts, knowledge files, app data. Credential values that the platform recognizes as belonging to the source install's own credentials are masked on a best-effort basis — see Secret scrubbing below; they are not unconditionally excluded, and anything the scrubber does not recognize is captured verbatim.

### Why prompts and memory are captured

The recipient's core question is *what was the system prompt for this run*, and for a bundle install they cannot answer it from their own copy. Two things drift:

- **Prompts drift.** A consumer can edit `WORKFLOW_PROMPT.md` in their own container; the edit flows back into their `Agent` row through the bidirectional prompt reconcile and stays there. The publisher only ever sees the text *they* published. Without the captured prompts a publisher debugs a prompt that was not the one running.
- **Memory never existed publisher-side at all.** `app-data/memory/*.md` is per-install personal content that is injected into every system prompt, is excluded from bundle snapshots and git by design, and never round-trips to the backend. The container is the only place it exists.

Two consequences follow from that asymmetry:

- **Divergence is computed, not guessed.** Each captured prompt is hashed and compared against the same field on the `AgentBundleRevision` the install was materialised from. With no installed revision behind the agent there is no baseline, and divergence is reported as **unknown** — never as "no", which would assert a match that was never checked. The archive's `prompts/README.md` renders the per-document result, and the recipient's detail modal shows the roll-up.
- **The roll-up covers the published prompt documents only.** `router_trigger` is routing metadata — it says *when to route to* the agent, the platform generates one for foreign installs that have none, and the install's owner is entitled to set their own. Counting it would report "the consumer edited your prompt" on close to every consumer install, for text no person wrote; a flag that is usually wrong gets ignored, and it would take the genuine workflow-prompt signal down with it. It is still shown per-document, marked *not compared*.
- **Memory is the one live read in the feature.** Everything else comes from the database; the memory area is read from the container over the env HTTP channel, once, at consent time, and frozen with the rest. It **never wakes a stopped container** — submitting a report must not start billable compute on the requester's behalf — so a stopped environment records `env_not_running` rather than memory content. Post-write, the no-live-read-through invariant holds exactly as before: nothing re-reads the container, ever.

The block records *why* it is empty (`declined_by_requester`, `no_environment`, `env_not_running`, `read_failed`, `empty`), because "the user opted out", "the container was off" and "this install has no notes" lead the recipient to different conclusions.

### Secret scrubbing

**Best-effort, not absolute.** Every string under `content`, `brief`, `result_summary`, or `title` in the frozen snapshot is checked against a secret set collected at capture time, and matches are replaced with `***REDACTED***` (longest value first, so a longer secret is masked before a shorter value that happens to be its prefix). The secret set is narrow by construction:

- Only values from `Credential` rows **linked to the source install** (via `AgentCredentialLink`), filtered through the shared `CredentialsService.SENSITIVE_FIELDS` map, plus the API keys of the AI credentials the install's environment is wired to. Credentials belonging to other agents or other users are never consulted.
- Values shorter than **8 characters** are dropped from the set before matching, to avoid shredding ordinary prose.
- Five keys are rewritten — `content`, `brief`, `result_summary`, `title`, and `text` (the prompt and memory documents). Both the `snapshot` **and** the `context` block are passed through the scrubber: the rest of the context is ids, names and settings, which are not under a scrubbed key and so pass through untouched, but the captured prompts and memory files are exactly the kind of free text a pasted endpoint or key hides in.
- The `sha256` recorded for each captured document is taken **before** masking, so it identifies the text as the agent actually ran it. A document containing `***REDACTED***` will therefore not re-hash to its recorded digest; the archive says so.
- Collecting the secret set is best-effort: a failure reading a credential or an AI credential is logged (by exception type only, never the value) and the request still goes through with whatever secrets were successfully collected — potentially an empty set.

The practical consequence: a secret the requester **typed into the conversation themselves** — a password, an API key pasted from somewhere else, a token that is not one of this install's own stored credentials — is invisible to the scrubber and is shared exactly as written. The consent modal states this explicitly in a dedicated warning, separate from the Included/Not-included lists, so a user cannot mistake "credential masking exists" for "everything sensitive is removed."

### Consent is final

There is no withdrawal path in v1. The only mutation surface after creation is the recipient's status/resolution-note update; the requester cannot edit, delete, or unshare a request they have submitted. The consent modal and the `/session-improve` confirmation both say this plainly before/at the point of submission.

## Access Control

| Action | Requester | Recipient (agent owner) | Anyone else |
|---|---|---|---|
| Preview eligibility / submit | own session only | — | 403 / 404 |
| List own submitted requests | ✅ | — | — |
| List requests on an agent | — | ✅ (owns agent) | 404 |
| View one request's detail / download archive | ✅ | ✅ (download audited) | 404 |
| Change status / resolution note | ❌ 403 | ✅ | 404 |
| Delete | ❌ 403 | ✅ | 404 |

Inaccessible request ids answer **404, not 403** — a 403 would confirm the id exists. The one deliberate exception is a requester attempting to *mutate* a row they are already known to be party to (they already know it exists), which answers 403.

## Integration Points

- **[Agent Sessions](../agent_sessions/agent_sessions.md)** — the source of the snapshot; the **Improve Agent** menu item lives on the session page, above **Edit Session**.
- **[Agent Commands](../../agents/agent_commands/agent_commands.md)** — `/session-improve` is a sync, non-LLM-context command, unavailable on guest/webapp-share sessions.
- **[Agent Bundles](../../agents/agent_bundles/agent_bundles.md)** — recipient resolution walks `bundle_uuid` → the publisher install; the frozen context records the installed vs. latest revision/version, so the archive's README can state whether an update was already pending.
- **[Agent Environments](../../agents/agent_environments/agent_environments.md)** — the frozen context records the environment name/version, instance, image tag + staleness, and critical state at capture time.
- **[Account CLI Workspace](../cinna_cli_integration/account_cli_workspace.md#improvement-requests-cinna-improve)** — `/account/improvement-requests*` endpoints back `cinna improve list|show|download|status`; the shipped guide teaches the local coding agent the whole loop.
- **[Realtime Events](../realtime_events/event_bus_system.md)** — `improvement_request_created` / `improvement_request_updated` are emitted to the **recipient's** user room so the Configuration-tab card updates live.
- **[Agent Credentials](../../agents/agent_credentials/agent_credentials.md)** — the secret scrubber reuses `CredentialsService.SENSITIVE_FIELDS`, the same per-type sensitive-field map credential redaction uses elsewhere.

## Not in v1

- **No withdrawal or retention expiry** — consent is final by product decision.
- **No snapshot refresh** — a second capture would need its own, separately-consented submission.
- **No email or Notification Catalog entry** — the only live signal is the WebSocket event and the Configuration-tab card badge.
- **No container-log attachment** — deferred because environment logs are per-environment and would leak the requester's other sessions.
- **No aggregate/roll-up view across a bundle's installs**, and no requester follow-up thread beyond the one-shot `resolution_note`.
- **No direct file attachment** on the request itself.
