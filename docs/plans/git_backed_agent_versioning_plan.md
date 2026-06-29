# Implementation Plan: Git-Backed Agent Versioning

> Backend-focused implementation plan derived from
> `drafts/git_backed_agent_versioning_suggestion.md`.
> The architectural decisions in that draft are **confirmed** — this document plans *how* to
> build them, grounded in the actual code seams. Frontend work is noted only at the client-regen
> level; no UI is planned in depth.

---

## 1. Overview

Let git be a **storage/transport backend and an external interchange format** for the thing the
platform already versions: an `AgentBundleRevision`. A git tree (`cinna.agent.json` + `workspace/`
+ `.gitignore`) is byte-for-byte the schema_version-2 bundle snapshot layout, so checkout / pull /
push reduce to operations the platform already performs, with git as the wire.

Core capabilities:
- **Checkout** — clone `<repo>[/subdir]@<ref>`, parse `cinna.agent.json`, create an `Agent` install
  + env, seed the workspace from the cloned tree.
- **Pull / update** — `git pull`, reuse `replace_bundle_content`, advance `last_synced_commit`;
  "update available" via `ls-remote` HEAD ≠ `last_synced_commit`.
- **Push** — capture the live workspace via `_snapshot_workspace_tree`, serialize the manifest,
  fast-forward-only commit + push.
- **Push-webhook (GitOps)** — a git-source webhook on the existing `agent_webhooks` infra triggers
  the pull path.

High-level flow:

```
remote git repo ──clone──▶ RevisionFormat.read ──▶ AgentBundleRevision (internal SSOT)
                                                          │
                                                          ▼
                                          install + env + seed_workspace_from_bundle_snapshot
                                                          │
  push ◀── git commit/ff-push ◀── RevisionFormat.write ◀──┤ (live workspace via _snapshot_workspace_tree)
                                                          │
  pull ──▶ git pull ──▶ replace_bundle_content ──────────┘ (advance last_synced_commit)
```

---

## 2. Architecture Overview

### Components (new + reused)

| Layer | New | Reused verbatim | Extended |
|-------|-----|------------------|----------|
| Format | `RevisionFormat` (de)serializer | manifest dict shape (publish_service L272-302) | — |
| Git I/O | — | `clone_repository`, `pull_repository`, `verify_repository_access`, `get_current_commit_hash`, `create_ssh_key_file`, URL converters (`git_operations.py`) | add `ls_remote_head`, `commit_all`, `fast_forward_push`; wire `egress_guard` |
| Workspace | — | `_snapshot_workspace_tree` (write), `seed_workspace_from_bundle_snapshot` (read), `replace_bundle_content` (update), `workspace_classification` (`.gitignore` / symlink guards), `snapshot_layout` | — |
| Auth | — | `SSHKeyService.get_decrypted_private_key`, `egress_guard.assert_url_allowed` | generalize egress guard out of `mcp_providers` |
| Model | `AgentGitSource` table | `AgentBundleRevision` / `AgentBundle` schema | — |
| Service | `GitSourceService` | `InstallService._install_from_revision`, `PublishService` snapshot/hash helpers | — |
| Routes | `agent_git.py` | `require_developer`, ownership-resolver pattern | — |
| GitOps | git-source webhook type | `AgentWebhookService` (Fernet token, logs), `agent_hooks.py` public dispatch | add type discriminator |

### Integration points
- **Bundles** remain the internal runtime SSOT (App Data keying by `bundle_id`, install counts,
  credential specs, schedule materialization). Git is the external face. Every git operation funnels
  through the same `AgentBundleRevision` rows and the same workspace copy primitives.
- **Knowledge sources** already use `git_operations.py` — extending it (commit/push, egress) benefits
  both. The model `AgentGitSource` is modeled on `AIKnowledgeGitRepo`.
- **SSH keys** — private-repo auth reuses the encrypted `UserSSHKey` + host-side temp-key pattern.
- **Webhooks** — the GitOps trigger rides `agent_webhooks` (no new transport).

---

## 3. Phase 1 — Refactor Seam: extract `RevisionFormat`

**Goal:** one (de)serializer for the canonical layout (`manifest.json` + `workspace/`), split out of
`PublishService` (write) and `InstallService` (read). **No behavior change.** Publish/install suites
must stay green. This is the seam git plugs into in later phases.

### Files
- **Create** `backend/app/services/bundles/revision_format.py`.
- **Modify** `backend/app/services/bundles/publish_service.py` (delegate snapshot/manifest writing).
- **Modify** `backend/app/services/environments/workspace_copy.py` (no signature change; route
  layout detection through the new module if it tightens the seam).
- **Modify** `backend/app/services/bundles/install_service.py` (read manifest via the new module
  where it constructs revision-shaped data — checkout reuses this in Phase 3).

### `RevisionFormat` API (descriptions; method signatures)
- `build_manifest(*, install, env, cred_specs, schedule_specs, plugin_specs, revision_number, version, release_notes) -> dict`
  — extract the exact dict currently built inline in `PublishService._publish_locked` (L272-302).
  Single source of the manifest schema (schema_version 2, prompts, sdk + model overrides,
  `required_credential_specs`, `schedules`, `plugin_specs`, `release_notes`).
- `write_tree(*, env_workspace_root: Path | None, dest: Path, manifest: dict, manifest_filename="manifest.json") -> str`
  — call `PublishService._snapshot_workspace_tree(env_workspace_root, dest)`, compute
  `content_hash` via the existing `_hash_tree_with_manifest`, write `<dest>/<manifest_filename>`.
  Returns the content hash. `manifest_filename` is the only git-vs-bundle difference
  (`cinna.agent.json` for git trees, `manifest.json` for bundle storage).
- `read_manifest(snapshot_path: Path) -> dict` — load the manifest, dispatching the filename
  (`manifest.json` then `cinna.agent.json`) and validating `schema_version`. Raises a typed
  `RevisionFormatError` on malformed/unsupported manifests.
- `manifest_to_revision_fields(manifest: dict) -> dict` — map manifest keys to
  `AgentBundleRevision` constructor kwargs (prompts, `agent_sdk_*`, `model_override_*`,
  `required_credential_specs`, `schedules`, `plugin_specs`, `version`, `release_notes`). This is the
  shape `InstallService._install_from_revision` already consumes from a revision row.
- `generate_gitignore() -> str` — emit `.gitignore` lines from
  `workspace_classification.BUNDLE_EXCLUDED_TOPLEVEL` + `RUNTIME_NAME_DENYLIST` +
  `PLUGIN_DERIVED_FILES`. **Single source of truth for what can never be committed.**

### Reuse vs. extend
- `_snapshot_workspace_tree`, `_copy_plugins_tree`, `_hash_tree_with_manifest`,
  `snapshot_layout`, `seed_workspace_from_bundle_snapshot`, `replace_bundle_content` — **reused
  verbatim** (called by the new module; bodies unchanged).
- `PublishService._publish_locked` — **modified** to delegate manifest construction + tree write to
  `RevisionFormat` (mechanical extraction, identical output).

### Validation
- Existing publish/install tests must pass unchanged (golden test of "no behavior change").
- Add unit tests for `RevisionFormat` round-trip: `write_tree` → `read_manifest` →
  `manifest_to_revision_fields` reproduces the input; `generate_gitignore` lists every denylisted
  top-level name.

---

## 4. Phase 2 — Model + Auth + Egress

**Goal:** the `AgentGitSource` table + migration; wire `egress_guard` into the clone path; extend
`git_operations.py` with `ls-remote HEAD`, commit, and fast-forward push.

### 4.1 Data model — `AgentGitSource`

**File:** `backend/app/models/bundles/agent_git_source.py` (re-export in
`backend/app/models/__init__.py`). Modeled on `AIKnowledgeGitRepo`.

Table `agent_git_source`:

| Field | Type | Notes |
|-------|------|-------|
| `id` | `uuid.UUID` PK | `default_factory=uuid.uuid4` |
| `agent_id` | `uuid.UUID` FK → `agent.id` `ondelete=CASCADE`, indexed | the install this source backs |
| `owner_id` | `uuid.UUID` FK → `user.id` `ondelete=CASCADE`, indexed | per-agent ownership scope |
| `bundle_uuid` | `uuid.UUID \| None` FK → `agent_bundle.id` `ondelete=SET NULL` | mirrors `Agent.bundle_uuid`; the git analog of bundle linkage |
| `repo_url` | `str` | HTTPS or SSH; normalized by URL converters |
| `subdir` | `str \| None` | path within repo (several agents per repo); `NULL` = repo root |
| `ref` | `str` default `"main"` | branch or tag |
| `ssh_key_id` | `uuid.UUID \| None` FK → `user_ssh_keys.id` `ondelete=SET NULL` | private-repo auth |
| `sync_direction` | `str` enum (`pull` / `push` / `bidirectional`) default `bidirectional` | governs which ops are allowed |
| `last_synced_commit` | `str \| None` | SHA; git analog of `installed_revision_id`; idempotency pin |
| `last_sync_at` | `datetime \| None` | |
| `status` | `str` enum (`pending` / `connected` / `error` / `disconnected`) default `pending` | reuse `SourceStatus` shape |
| `last_error` | `str \| None` (`Text`) | last failure detail |
| `created_at` / `updated_at` | `datetime` | `default_factory=now(UTC)` |

Indexes:
- `agent_id` (btree) — one-source lookup per agent.
- Unique `(agent_id)` — **one git source per install** (enforce single backing repo; revisit if
  multi-remote ever needed — Open Question).
- `owner_id` (btree).

Model variants (project convention):
- `AgentGitSourceBase(SQLModel)` — `repo_url`, `subdir`, `ref`, `ssh_key_id`, `sync_direction`.
- `AgentGitSource(AgentGitSourceBase, table=True)` — adds ids, status, `last_synced_commit`,
  `last_sync_at`, `last_error`, timestamps.
- `AgentGitSourcePublic(AgentGitSourceBase)` — adds `id`, `agent_id`, `owner_id`, `bundle_uuid`,
  `status`, `last_synced_commit`, `last_sync_at`, `last_error`, timestamps, computed
  `update_available: bool` (set by the route from a cheap `ls-remote` when requested).
  **Never** include SSH key material.
- `AgentGitSourceCreate(SQLModel)` — `repo_url`, `subdir`, `ref`, `ssh_key_id`, `sync_direction`
  (used by checkout body, see Phase 3).
- `AgentGitSourceUpdate(SQLModel)` — all optional: `repo_url`, `subdir`, `ref`, `ssh_key_id`,
  `sync_direction`.

### 4.2 Migration

**File:** `backend/app/alembic/versions/<hash>_add_agent_git_source.py`.
- `revision` = new hash; `down_revision` = current head — **run `alembic heads` first**; if multiple
  heads exist (pre-existing condition in this repo), add a merge migration rather than guessing.
- `upgrade`: `create_table("agent_git_source", ...)` with the columns above; FKs with the stated
  `ondelete`; create the indexes (incl. the unique `agent_id`).
- `downgrade`: drop indexes then `drop_table`.
- Enum columns stored as plain `str` (`sa.String`) with app-level validation (consistent with
  `AgentWebhook.type` and `SourceStatus` usage) — avoids pg enum migration churn.

### 4.3 Egress guard generalization

The SSRF chokepoint `assert_url_allowed` currently lives in
`backend/app/services/mcp_providers/egress_guard.py` and keys off `MCP_PROVIDER_ALLOW_PRIVATE_HOSTS`.

- **Move/promote** the guard to a neutral location (e.g. `backend/app/core/egress_guard.py` or
  `backend/app/services/common/egress_guard.py`) and re-export from the old path to avoid breaking
  MCP imports. Keep `assert_url_allowed`, `validate_external_endpoint_url`, `is_host_blocked`
  unchanged.
- Add a setting alias so git clones honor the same private-host policy
  (reuse `MCP_PROVIDER_ALLOW_PRIVATE_HOSTS` or add `GIT_SOURCE_ALLOW_PRIVATE_HOSTS` that defaults to
  the same value — Open Question on naming).
- **Note:** SSH URLs (`git@host:owner/repo.git`) have no scheme; `assert_url_allowed` only accepts
  `http(s)`. The guard must run on the **resolved host**, not the raw scheme. Plan: parse the host
  out of both HTTPS and SSH forms (reuse the regex in `convert_ssh_to_https_url`) and call
  `is_host_blocked(host)` directly for SSH, `assert_url_allowed(url)` for HTTPS. Wire this into
  `clone_repository` / `verify_repository_access` / `pull_repository` before any network call.

### 4.4 `git_operations.py` extensions

Add (host-side, GitPython), preserving the SSH-key temp-file + `GIT_SSH_COMMAND` + `finally`-cleanup
pattern already used:
- `ls_remote_head(git_url, ref, ssh_key_path=None) -> str` — return the remote ref's SHA via
  `git ls-remote <url> refs/heads/<ref>` (extend the existing `verify_repository_access` ls-remote
  call). Used for "update available". **Egress-guarded.**
- `commit_all(repo, message, author_name, author_email) -> str` — `repo.git.add(A=True)`,
  `repo.index.commit(...)`, return new SHA. No-op safe when the tree is unchanged (return current
  HEAD).
- `fast_forward_push(repo, ref, ssh_key_path=None) -> None` — fetch remote `ref`, assert the local
  branch is ahead-of-or-equal (ff-only); push with `GIT_SSH_COMMAND`. Raise a typed
  `GitNonFastForwardError(GitOperationError)` when the remote advanced. **Egress-guarded.**
- `clone_repository` gains an explicit `subdir` awareness only at the service layer (clone whole
  repo, operate on `<clone>/<subdir>`); no change to the clone primitive itself.
- For push, clone **without** `depth=1` (or `--no-single-branch` + unshallow) — ff-push needs the
  ref history. Keep checkout/pull shallow.

### Validation
- Unit tests for `ls_remote_head` (mock GitPython), `fast_forward_push` raising on non-ff, egress
  guard rejecting a private-host repo URL (both HTTPS and SSH forms).

---

## 5. Phase 3 — Checkout (read path)

**Goal:** `POST /agents/checkout` — clone a repo/subdir, import its `cinna.agent.json` as an internal
bundle revision, create an install + env, seed the workspace from the cloned tree.

### Files
- **Create** `backend/app/api/routes/agent_git.py` (router prefix `/agents`, tags `["agent-git"]`),
  register in `backend/app/api/main.py`.
- **Create** `backend/app/services/bundles/git_source_service.py` (`GitSourceService`).

### Route
```
POST /agents/checkout            dependencies=[Depends(require_developer)]
```
- Body `AgentCheckoutRequest(SQLModel)`: `repo_url`, `subdir: str|None`, `ref: str="main"`,
  `ssh_key_id: uuid.UUID|None`, `sync_direction: str="bidirectional"`,
  optional `name_override: str|None`.
- DI: `SessionDep`, `CurrentUser`, `require_developer`.
- Returns `AgentPublic` (the created install) plus the `AgentGitSourcePublic` (envelope or
  combined response model `AgentCheckoutResponse`).
- Thin controller: resolve `ssh_key_id` ownership against `current_user`, call
  `GitSourceService.checkout(...)`, translate `GitOperationError` / `RevisionFormatError` to 4xx.

### `GitSourceService.checkout` (signature + flow)
```
async def checkout(*, session, user, repo_url, subdir, ref, ssh_key_id, sync_direction, name_override) -> tuple[Agent, AgentGitSource]
```
1. **Egress + auth:** guard `repo_url`; if `ssh_key_id` set, `SSHKeyService.get_decrypted_private_key`
   (ownership-checked) → host-side temp key file via `create_ssh_key_file` (deleted in `finally`).
2. **Clone** (shallow) into a temp dir via `clone_repository_context`; `src = <clone>/<subdir or ".">`.
   Record `last_synced_commit = get_current_commit_hash(repo)`.
3. **Validate** the tree is a v2 snapshot: `snapshot_layout(src) == "v2_workspace"` and
   `RevisionFormat.read_manifest(src)` succeeds. Reject otherwise (400, "not a Cinna agent repo").
4. **Import as bundle revision (internal SSOT):**
   - Resolve `bundle_id` — **decision point** (see Open Questions). Default plan: reuse the
     manifest's `bundle_id` so App Data reattaches across checkouts of the same agent; but the
     checking-out user is **not** the publisher, so create a *consumer-style* `AgentBundle` row
     (or look up an existing one) keyed by that `bundle_id` with `publisher_user_id = NULL` /
     `visibility = private`, and the checkout user as owner of the install only.
   - Persist the cloned tree into bundle storage as the revision's `snapshot_path` (copy `src` →
     `<BUNDLE_STORAGE_DIR>/<bundle_id>/<rev>/`, writing `manifest.json` via
     `RevisionFormat.write_tree`-style serialization, or simply move the validated tree). Build the
     `AgentBundleRevision` via `RevisionFormat.manifest_to_revision_fields(manifest)`.
5. **Install:** reuse `InstallService._install_from_revision(session, user, bundle, revision, request=None)`
   — this creates the `Agent`, env (auto-start), seeds workspace from `revision.snapshot_path` via
   `seed_workspace_from_bundle_snapshot`, materializes schedules/plugins/credential specs. **No new
   seeding logic** — the cloned tree *is* a v2 snapshot.
6. **Record the source:** create `AgentGitSource(agent_id=install.id, owner_id=user.id,
   bundle_uuid=bundle.id, repo_url, subdir, ref, ssh_key_id, sync_direction,
   last_synced_commit, status="connected", last_sync_at=now)`.
7. Apply `name_override` to the install if provided; commit; return.

### Security / invariants honored
- `ssh_key_id` ownership verified against `current_user` (no cross-user key use).
- Egress guard on the repo URL before clone.
- Private key only ever a chmod-600 temp file, never copied into the container.
- `required_credential_specs` arrive metadata-only (same rule the manifest already enforces); the
  checkout user fills secrets via the existing install-credentials UI.

### Validation
- Checkout a public HTTPS repo containing a valid v2 tree → install + running env + workspace seeded.
- Checkout with `subdir` → only that subdir imported.
- Reject a repo without `cinna.agent.json` / non-v2 layout (400).
- Reject a private repo without/with-wrong ssh key (auth error mapped to 4xx).
- Reject a repo URL resolving to a private host (egress).

---

## 6. Phase 4 — Pull Update (manual button)

**Goal:** `POST /agents/{id}/git/pull` reusing `replace_bundle_content`; "update available" via
`ls-remote`.

### Routes
```
GET  /agents/{agent_id}/git                dependencies: owner-resolved      -> AgentGitSourcePublic (+ update_available)
POST /agents/{agent_id}/git/pull           dependencies=[Depends(require_developer)] -> AgentPublic
GET  /agents/{agent_id}/git/check-updates  owner-resolved                    -> {update_available, remote_commit, last_synced_commit}
```
- Reuse the install-ownership resolver pattern from `installs.py` (`_resolve_install_owned`) — copy
  it into `agent_git.py` or import a shared helper. Pull is **developer-gated**; read endpoints are
  owner-gated.

### `GitSourceService` methods
- `check_updates(session, agent_id, owner) -> GitUpdateStatus`
  - Resolve the `AgentGitSource`; decrypt ssh key if any; `ls_remote_head(repo_url, ref)` (egress
    guarded). `update_available = remote_sha != last_synced_commit`. Cheap, no clone. Mirrors
    `InstallService.check_for_updates` / `pending_update` semantics.
- `pull_update(session, agent_id, owner) -> Agent`
  1. **Per-agent lock** (`asyncio.Lock` keyed by `agent_id`; same pattern as
     `PublishService._lock_for`). Prevents concurrent pull/push on one agent.
  2. **Fail-loud dirty guard:** the file side is protected by the `replace_bundle_content`
     denylist (App Data / credentials / logs / databases / uploads / consumer plugins preserved), so
     bundle-owned content is authoritatively replaced. For the **manifest/DB side**, start fail-loud:
     if the live install's prompts/sdk differ from `last_synced_commit`'s manifest (i.e. local
     uncommitted changes exist), block with a clear "push or discard local changes first" 409.
     (3-way reconcile is Phase 5 / Open Question.)
  3. Clone repo/subdir at `ref` into temp; `remote_sha = get_current_commit_hash`.
  4. Re-import manifest → new `AgentBundleRevision` (same bundle, `revision_number+1`), persist the
     pulled tree as `snapshot_path` (as in checkout step 4).
  5. **Reuse `InstallService.apply_update`** path semantics, OR call `replace_bundle_content(
     Path(revision.snapshot_path), env.id)` directly on the active env (the suggestion says reuse
     `replace_bundle_content` verbatim). Update DB prompt/sdk fields from the manifest
     (checkout/pull = **DB-from-manifest** direction).
  6. Advance `last_synced_commit = remote_sha`, `last_sync_at = now`, `status="connected"`.
- Direction guard: `pull` allowed when `sync_direction in (pull, bidirectional)`.

### Reuse vs. extend
- `replace_bundle_content` — **reused verbatim** (denylist merge/prune already preserves
  App Data/creds/plugins, prunes stale bundle-owned dirs).
- `ls_remote_head` — from Phase 2.
- `AgentBundleRevision` creation — reuse `RevisionFormat.manifest_to_revision_fields`.

### Validation
- Pull when remote advanced → workspace updated, prompts/sdk synced, `last_synced_commit` advanced,
  App Data + credentials preserved, stale bundle dirs pruned.
- `check-updates` reflects HEAD≠last_synced.
- Pull with local uncommitted manifest changes → 409 fail-loud.
- Pull on a `push`-only source → 400.

---

## 7. Phase 5 — Push (bidirectional)

**Goal:** `POST /agents/{id}/git/push` via `_snapshot_workspace_tree`; fast-forward-only;
fail-loud conflict guard; optionally cut a parallel bundle revision.

### Route
```
POST /agents/{agent_id}/git/push   dependencies=[Depends(require_developer)]   -> AgentGitSourcePublic
```
- Body `GitPushRequest(SQLModel)`: `commit_message: str`, `version: str|None`,
  `also_publish_bundle: bool=False`.

### `GitSourceService.push` (flow)
1. **Per-agent lock** (shared with pull).
2. Direction guard: `sync_direction in (push, bidirectional)`.
3. **ff precheck:** `ls_remote_head(repo_url, ref)`; if `remote_sha != last_synced_commit` → 409
   "remote advanced — pull first" (do not clone/commit).
4. Clone repo/subdir at `ref` (full history, not shallow) into temp; decrypt ssh key as needed.
5. **Capture the live workspace:** resolve the active env's workspace root; call
   `RevisionFormat.write_tree(env_workspace_root=<env app/workspace parent>, dest=<clone>/<subdir>,
   manifest=RevisionFormat.build_manifest(...))`. This reuses `_snapshot_workspace_tree`
   **verbatim**, so the denylist + symlink guards apply — credentials/app-data/logs/databases/
   uploads can never be written into the git tree. Write `cinna.agent.json` (manifest filename) and
   refresh `.gitignore` via `RevisionFormat.generate_gitignore()`.
   - **Size guard** (Open Question): reject / warn when a captured `files/` asset exceeds a
     configured threshold before committing (binary-in-git hygiene).
6. `commit_all(repo, commit_message, author=<owner identity>)`; if nothing changed, short-circuit
   (no empty commit, return current SHA).
7. `fast_forward_push(repo, ref)`; on `GitNonFastForwardError` → 409.
8. Advance `last_synced_commit = new_sha`, `last_sync_at = now`. (push = **manifest-from-DB**
   direction.)
9. If `also_publish_bundle`: call `PublishService.publish(...)` so the internal bundle mirror and the
   git tree stay in lockstep (optional, keeps installs + git aligned).

### Conflict / concurrency (honors draft §Concurrency)
- Per-agent `asyncio.Lock` serializes pull/push.
- Push is ff-only via `ls-remote` precheck + `fast_forward_push` (double guard).
- Dirty-pull is fail-loud (Phase 4). 3-way reconcile for manifest/prompt fields is the documented
  follow-on (Open Question — depth).
- `last_synced_commit` is the idempotency pin (git analog of `.cinna_plugin_ref` /
  `installed_revision_id`).

### Validation
- Push with local workspace edits → new commit on remote, `.gitignore` present, no
  credentials/app-data in the tree, `last_synced_commit` advanced.
- Push when remote advanced → 409, no commit.
- Push with `also_publish_bundle=true` → parallel `AgentBundleRevision` created.
- Symlink in workspace pointing at `../credentials` → never committed (covered by `safe_copytree`).

---

## 8. Phase 6 — Push-webhook (GitOps)

**Goal:** a git-source webhook on `agent_webhooks` triggers the pull path. No new transport.

### Approach
Extend the existing webhook infra with a third type discriminator alongside `session` / `script`.

- **Model** `backend/app/models/agents/agent_webhook.py`: add `AgentWebhookType.GIT_SOURCE = "git_source"`
  and `AgentWebhookCreateGitSource(SQLModel)` (just `name`, `type`, optional `payload_template`).
  No new columns required — a git-source webhook needs only the existing token + `agent_id`.
- **Service** `backend/app/services/agents/agent_webhook_service.py`:
  - `create_git_source_webhook(...)` mirroring `create_session_webhook` (Fernet token, prefix,
    one-time reveal).
  - In `fire_webhook`, dispatch `type == "git_source"` → call
    `GitSourceService.pull_update(session, webhook.agent_id, owner=<webhook owner>)`. Reuse the
    immutable invocation-log write + the "always 200 with log_id post-auth" contract. Payload body
    is ignored (or used only to assert `ref` matches, optional).
- **Public dispatch** `backend/app/api/routes/agent_hooks.py` — **unchanged**; it already routes by
  `webhook.type` through `fire_webhook`. The git push provider (GitHub/GitLab) calls
  `{host}/agent-hooks/{webhook_id}` with the bearer token; the handler pulls.
- **Route** `agent_webhooks.py`: add `POST /agents/{agent_id}/webhooks/git-source`
  (developer-gated — creating a GitOps trigger is a developer action).

### Security
- Reuses Fernet-encrypted bearer token, `hmac.compare_digest` validation, 64KB payload cap,
  immutable logs — all inherited from `AgentWebhookService`.
- The pull it triggers is itself egress-guarded + ff-safe + per-agent-locked (Phases 2/4).

### Validation
- Create a git-source webhook → fire it with the token → install pulls latest, log row written.
- Bad token → 401; disabled webhook → 4xx; pull failure → 200 with error-status log (post-auth
  contract).

---

## 9. Security Architecture (cross-phase, honors all draft invariants)

- **SSH key host-side only:** `SSHKeyService.get_decrypted_private_key` → `create_ssh_key_file`
  (chmod 600) → `GIT_SSH_COMMAND` → deleted in `finally`. Never copied into the container. Key
  material never appears in any Public model or log.
- **Egress guard mandatory on clone/pull/push/ls-remote** — single chokepoint
  (`assert_url_allowed` / `is_host_blocked` on the resolved host) wired into `git_operations.py`.
  Closes the current gap (knowledge/plugin clones do no SSRF check today; retrofitting them is an
  Open Question to decide in the same pass).
- **`.gitignore` + symlink guards load-bearing:** `RevisionFormat.generate_gitignore()` is derived
  from `BUNDLE_EXCLUDED_TOPLEVEL`; `_snapshot_workspace_tree` + `safe_copytree` + `_ignore_symlinks`
  guarantee `credentials/`, `app-data/`, `logs/`, `databases/`, `uploads/` can never be committed,
  even via a symlink. Push reuses these verbatim — the guarantee is structural, not re-implemented.
- **`required_credential_specs` metadata-only** — same rule
  `PublishService._template_payload_for` already enforces (private field values stripped). Carried
  through `RevisionFormat` unchanged.
- **Per-agent ownership; no monorepo** — `AgentGitSource.owner_id` + per-install resolver; `subdir`
  gives "several agents per repo" without collapsing tenant ACL into one history.

---

## 10. Error Handling & Edge Cases

- **Malformed `cinna.agent.json` / non-v2 tree** → `RevisionFormatError` → 400.
- **Auth failure (private repo, bad key)** → `GitAuthenticationError` → 401/403.
- **Connection / unresolvable host** → `GitConnectionError` → 502/400.
- **Egress-blocked URL** → `EgressBlockedError` → 400.
- **Non-fast-forward push** → `GitNonFastForwardError` → 409 ("pull first").
- **Dirty manifest on pull** → 409 fail-loud (Phase 4).
- **No active env on push** → 400 ("start the environment first") — mirrors
  `PublishService._assert_workspace_readable`.
- **Oversized binary asset** → 413/400 with the offending path (size guard, Open Question).
- **Concurrent pull/push** → serialized by the per-agent lock; second caller waits.
- **Partial clone failure** → temp dir cleaned up (`clone_repository` already `rmtree`s on error;
  `clone_repository_context` cleans the temp tree in `finally`).

---

## 11. Integration Points

- **API client regen:** after each phase that adds routes (3–6), run
  `source ./backend/.venv/bin/activate && make gen-client` so the frontend gets the new
  `AgentGitService`. No deep frontend UI is planned here; the eventual UI ("Checkout", "Update
  available" badge, "Push" button) mirrors the bundle install/apply-update/publish UX.
- **Models re-export:** add `AgentGitSource*` to `backend/app/models/__init__.py`.
- **Router registration:** mount `agent_git.py` in `backend/app/api/main.py`; add the git-source
  webhook route in the existing `agent_webhooks.py` registration.
- **Bundle storage:** checkout/pull persist snapshots under the existing `BUNDLE_STORAGE_DIR` root
  (same as publish), so `snapshot_path` semantics are unchanged.
- **No agent-env (container) changes** — all git I/O is host-side; the container only ever receives a
  seeded workspace via the existing copy primitives.

---

## 12. Future Enhancements (Out of Scope)

- 3-way reconcile for manifest/prompt fields on dirty pull (beyond fail-loud) — reuse the
  prompt-sync `decide()` prior art.
- `WorkspaceSource` strategy abstraction (`BundleSnapshotSource` / `GitWorkspaceSource`) — only if
  seed/replace/checkout branch on origin in more than one place.
- Multiple git remotes per install; git-LFS for large assets.
- Auto-pull on a schedule (cron) in addition to manual + webhook.

---

## 13. Summary Checklist

### Phase 1 — Refactor seam
- [ ] Create `revision_format.py`: `build_manifest`, `write_tree`, `read_manifest`,
      `manifest_to_revision_fields`, `generate_gitignore`, `RevisionFormatError`.
- [ ] Refactor `PublishService._publish_locked` to delegate to `RevisionFormat` (no behavior change).
- [ ] Route install-side manifest reads through `RevisionFormat`.
- [ ] Publish/install suites stay green; add `RevisionFormat` round-trip unit tests.

### Phase 2 — Model + auth + egress
- [ ] Create `AgentGitSource` model (Base/Public/Create/Update) + re-export.
- [ ] Alembic migration `add_agent_git_source` (FKs, unique `agent_id`, indexes); confirm single head.
- [ ] Promote `egress_guard` to a neutral module (re-export from `mcp_providers`); host-extraction for
      SSH URLs.
- [ ] Extend `git_operations.py`: `ls_remote_head`, `commit_all`, `fast_forward_push`
      (`GitNonFastForwardError`); wire egress guard into clone/pull/push/ls-remote.

### Phase 3 — Checkout
- [ ] `GitSourceService.checkout`; create `agent_git.py` route `POST /agents/checkout`
      (`require_developer`).
- [ ] Reuse `InstallService._install_from_revision` + `seed_workspace_from_bundle_snapshot`.
- [ ] `bundle_id` adoption decision wired (see Open Questions).
- [ ] Register router; regen client.

### Phase 4 — Pull update
- [ ] `GitSourceService.check_updates` + `pull_update` (per-agent lock; fail-loud dirty guard).
- [ ] Routes `GET /agents/{id}/git`, `GET /git/check-updates`, `POST /git/pull` (pull
      developer-gated).
- [ ] Reuse `replace_bundle_content` verbatim; advance `last_synced_commit`.

### Phase 5 — Push
- [ ] `GitSourceService.push` (ff precheck + `fast_forward_push`; `_snapshot_workspace_tree` via
      `RevisionFormat.write_tree`; size guard; optional `also_publish_bundle`).
- [ ] Route `POST /agents/{id}/git/push` (`require_developer`).

### Phase 6 — Push-webhook (GitOps)
- [ ] `AgentWebhookType.GIT_SOURCE` + `AgentWebhookCreateGitSource` + service create/dispatch.
- [ ] `fire_webhook` routes `git_source` → `GitSourceService.pull_update`.
- [ ] Route `POST /agents/{id}/webhooks/git-source` (developer-gated); public `agent_hooks.py`
      unchanged.

### Testing & validation (per draft)
- [ ] RevisionFormat round-trip equals legacy publish/install output.
- [ ] Checkout (public + private + subdir + invalid-tree + egress-blocked).
- [ ] Pull (advance / no-change / dirty-409 / wrong-direction-400 / App-Data-preserved).
- [ ] Push (ff-success / non-ff-409 / symlink-never-committed / credentials-never-committed /
      parallel-bundle).
- [ ] GitOps webhook (fire → pull → log; bad token 401).

---

## 14. Open Questions / Decision Points (for the user)

1. **Checkout `bundle_id` identity.** Adopt the manifest's `bundle_id` (so App Data — keyed by
   `(user_id, bundle_id, catalog_type)` — reattaches across checkouts of the same agent) **or**
   generate a fresh `bundle_id` for the checking-out user? Adopting it means a non-publisher gets an
   install under someone else's reverse-DNS namespace; generating a new one breaks App Data
   continuity. **Recommendation:** adopt the manifest `bundle_id` but create the `AgentBundle` row as
   `private` / `publisher_user_id = NULL` (a local import, not a catalog publish). Needs sign-off.
2. **Does checkout create a full `AgentBundle` + `AgentBundleRevision`** (internal SSOT, consistent
   with "git is the external layer over bundles") **or** a lighter install with no bundle row? The
   plan assumes the former (so install counts / App Data / credential specs / schedules all work).
   Confirm.
3. **Bidirectional conflict UX depth** — ship fail-loud only (Phases 4/5) and defer 3-way reconcile,
   or build the prompt-sync-style reconcile now? Plan defers it.
4. **Manifest ↔ DB drift direction** is fixed per op (checkout/pull = DB-from-manifest; push =
   manifest-from-DB). Confirm this is the desired contract (it is the prompt-sync hazard, handled
   explicitly).
5. **Large/binary `files/` assets** — set a size threshold for the push guard (e.g. per-file MiB
   cap)? What value, and hard-reject vs warn?
6. **Retrofit the egress guard onto existing knowledge/plugin clones** in the same pass? Low effort
   once the guard is generalized; closes the same SSRF gap there. Recommended yes.
7. **Egress private-host policy setting** — reuse `MCP_PROVIDER_ALLOW_PRIVATE_HOSTS` or introduce a
   dedicated `GIT_SOURCE_ALLOW_PRIVATE_HOSTS`? (Self-hosted instances may host git on a private LAN.)
8. **One git source per install** (unique `agent_id`) — acceptable constraint, or is multi-remote a
   near-term need?
9. **Push clone depth** — push needs ref history for ff (non-shallow clone per push). Confirm the
   clone-per-push (ephemeral) model vs. a persisted working copy. Plan uses ephemeral.
