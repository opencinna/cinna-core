# Git-Backed Agent Versioning — Extension Plan

> **Status:** Draft implementation plan. **Continues uncommitted foundation work.**
>
> The git-versioning foundation (checkout / pull / push / check-updates / GitOps
> webhook, `AgentGitSource` model, `RevisionFormat`, promoted `egress_guard`,
> extended `git_operations`, two migrations) already exists in the working tree
> but is **not committed**. Because none of it is committed, this plan may
> **freely modify** any foundation file — there is **no backward-compatibility
> constraint** with the pre-extension state. Where this plan reshapes an existing
> service/route/model, that is intentional and preferred over preserving the
> current API surface for its own sake.
>
> **Foundation docs:** `docs/agents/agent_git_versioning/agent_git_versioning.md`
> + `_tech.md`; foundation plan `docs/plans/git_backed_agent_versioning_plan.md`.

---

## 0. Confirmed architectural decisions (do NOT relitigate)

1. **git = preservation/versioning layer; Mutagen = live runtime sync.** They are
   orthogonal. A local folder can be **both** a Mutagen sync target **and** a git
   working tree. Mutagen keeps editing the running container; git captures durable
   versioned snapshots. Nothing in this plan changes the Mutagen path.
2. **External git only.** No platform-hosted/internal repos. The backend
   pushes/pulls the external remote **host-side** via a deploy key; the
   developer's machine uses their **own** git/SSH client. The external remote is
   the single meeting point.
3. **Decision C — connect/enable is a distinct flow, not an overload of checkout.**
   `checkout` keeps importing a *foreign* repo into a *new* install. A new
   `connect` flow attaches a git source to an agent the user **already** has and
   performs an **initial export push** (first commit = current live workspace).
4. **Decision D — two-writer model.** Backend (deploy key) and developer machine
   (own creds) both push to the **same** external remote; both fast-forward-only;
   conflicts surface fail-loud (matches existing push behavior).

---

## 1. What already exists (reuse surface)

| Reusable unit | Location | How the extension uses it |
|---|---|---|
| `GitSourceService.checkout/pull_update/push/get_source/check_updates` | `backend/app/services/bundles/git_source_service.py` | Add `connect`, `list_commits`, `compute_dirty`; refactor shared internals |
| `_push_locked` capture path | same file | Initial-export push reuses the manifest-build + `write_tree` + `commit_all` + ff-push sequence |
| `_resolve_or_create_bundle`, `_persist_revision`, `_next_revision_number` | same file | Connect resolves/creates the backing bundle; same as checkout |
| `_resolve_ssh_key`, `_resolve_subdir`, `_assert_no_oversized_files`, `_read_and_validate_tree` | same file | Reused verbatim by connect + commit list |
| `_assert_not_dirty` (prompt DB drift) | same file | Refactor into a **boolean** `_prompts_dirty` reused by the dirty endpoint |
| `RevisionFormat.build_manifest / write_tree / generate_gitignore` | `backend/app/services/bundles/revision_format.py` | Initial push + dirty check (workspace capture) |
| `PublishService._snapshot_workspace_tree / _hash_tree_with_manifest / _assert_workspace_readable / _collect_*_specs` | `backend/app/services/bundles/publish_service.py` | Dirty check (workspace-only digest), initial push manifest |
| `git_operations.ls_remote_head / commit_all / fast_forward_push / clone_repository_context / get_current_commit_hash` | `backend/app/services/knowledge/git_operations.py` | Add `git_log_subdir`, `init_repo_with_remote` (empty-remote bootstrap) |
| `assert_git_url_allowed` (SSRF chokepoint) | same file | Every new network call goes through it |
| `SSHKeyService.get_decrypted_private_key / generate_key_pair / get_user_keys` + `SshKeysService` (FE) | `backend/app/services/users/ssh_key_service.py`, `frontend/src/client` | Deploy-key picker + quick-generate (Piece 2) |
| `CLIContextDep`, `cli_ctx.agent` | `backend/app/api/deps.py`, `backend/app/api/routes/cli.py` | Piece 4 CLI coordinates endpoint |
| `AgentIntegrationsTab` card grid | `frontend/src/components/Agents/AgentIntegrationsTab.tsx` | Mount the new GIT Versioning card |
| Foundation route file | `backend/app/api/routes/agent_git.py` | Add connect / commits / dirty routes + error mapping (already complete) |

**Confirmed facts that shape the design:**

- Every `Agent` has a non-null `bundle_id: str` (generated). `bundle_uuid` is
  nullable (NULL until published/linked). `is_publisher_install` distinguishes
  publisher vs consumer slot. (`backend/app/models/agents/agent.py`)
- `_hash_tree_with_manifest` hashes the **entire** manifest body except
  `content_hash` — including `revision_number`, `published_at`, `version`. So a
  revision's `content_hash` is **not** a stable dirty signal (it changes every
  build). The dirty check therefore uses a **workspace-files-only** digest plus
  the existing prompt-DB drift check. (See Phase 2.)
- `AgentGitSource` has a **unique** `agent_id` index — one git source per install.
  Connect must reject when a source already exists.
- The live env workspace root is `Path(settings.ENV_INSTANCES_DIR) / str(env.id)`.

---

## 2. Schema / migration decision

**No new migration is required.** Justification:

- `AgentGitSource` already carries `status` (`pending`/`connected`/`error`/
  `disconnected`), `last_synced_commit`, `last_sync_at`, `last_error`,
  `sync_direction`, `ssh_key_id`, `subdir`, `ref`, `bundle_uuid`. These cover the
  enable/disable, connect, dirty, and commit-history needs.
- **"Enabled" is modeled by row presence + `status`, not a new boolean.** The UI
  card is "disabled by default" = **no `AgentGitSource` row**. Enabling = connect
  creates the row (`status=connected`). Disabling = **delete the row** (Decision:
  delete, not a soft `disconnected` flag — see Open Questions Q1). This keeps the
  one-row-per-agent unique constraint meaningful and avoids a migration.
- Commit history and dirty state are **computed live** (git `ls-remote` / `git
  log` / workspace digest), never persisted — no columns needed.

If Q1 resolves toward soft-disable instead of delete, the existing `status`
column already has a `DISCONNECTED` value — still **no migration**.

---

## Phase A — Piece 1: Enable-on-existing-agent (`connect`) backend

### A.1 New git_operations primitive (empty-remote bootstrap)

**File:** `backend/app/services/knowledge/git_operations.py` (modify)

```python
def init_repo_with_remote(
    *, workdir: str, repo_url: str, ref: str = "main",
    ssh_key_path: str | None = None,
) -> Repo:
    """git init a fresh working tree, add `origin`, create branch `ref`.

    For the empty-remote / absent-ref bootstrap case where there is nothing to
    clone. Egress-guarded via assert_git_url_allowed before the remote is added.
    Returns a Repo with no commits yet; caller writes the tree, commit_all,
    fast_forward_push (which creates the branch on the remote).
    """
```

- Egress guard runs before `origin` is added. URL conversion mirrors
  `clone_repository` (SSH if key present, HTTPS otherwise).
- `fast_forward_push` already pushes without `--force`; pushing a brand-new
  branch to an empty remote is a fast-forward (no remote ancestor) and succeeds.
  Verify `fast_forward_push`'s merge-base check treats "remote ref absent" as
  ancestor-OK (if it currently raises, add an explicit "remote ref missing ⇒
  first push" branch — **modify** `fast_forward_push`, allowed since uncommitted).

### A.2 Service method `GitSourceService.connect`

**File:** `backend/app/services/bundles/git_source_service.py` (modify)

```python
@staticmethod
async def connect(
    *, session: Session, agent_id: uuid.UUID, user: User,
    repo_url: str, subdir: str | None, ref: str,
    ssh_key_id: uuid.UUID | None, sync_direction: str,
    commit_message: str = "Initial export from Cinna",
) -> tuple[AgentGitSource, Agent]:
    """Attach a git source to an EXISTING owned install + initial export push."""
```

**Flow (per-agent locked, mirrors `_push_locked` for the capture half):**

1. `_lock_for(str(agent_id))`.
2. Resolve the install owned by `user` (404 if missing/not owned — reuse the
   ownership pattern from `_resolve_source_owned`, but for the `Agent` row;
   superuser bypass consistent with existing code).
3. **No-existing-source guard:** `select(AgentGitSource).where(agent_id==...)` →
   if a row exists, raise `GitSourceConflictError` ("git source already
   configured; disconnect first") → **409** (and the route also catches the
   unique-constraint `IntegrityError` as a race backstop).
4. **Env-readable guard:** require an active env whose workspace is readable
   (`PublishService._assert_workspace_readable`) → else `GitSourceValidationError`
   → 400 ("start the environment so its workspace can be exported").
5. **Direction guard:** initial export is a write, so `sync_direction` must be
   `push` or `bidirectional`; `pull`-only at connect → 400.
6. `_resolve_ssh_key(session, ssh_key_id, user.id)` (ownership-checked temp key).
7. **Resolve backing bundle:**
   - if `install.bundle_uuid` is set → use that bundle (the real backing bundle);
   - else → `_resolve_or_create_bundle(bundle_id=install.bundle_id, user,
     display_name=install.name)` and set `source.bundle_uuid = bundle.id`.
     **Do not mutate `install.bundle_uuid`** (keeps publisher/consumer semantics
     intact; see Open Questions Q2).
8. **Remote-state probe** (`ls_remote_head`, egress-guarded), three branches:
   - **empty remote / ref absent** (`GitOperationError` "ref not found") →
     `init_repo_with_remote` path: init, write tree, commit, push (creates ref).
   - **ref exists but subdir is empty / absent** → full-history
     `clone_repository_context(depth=None)`, write tree into `subdir`, commit,
     ff-push on top of remote history.
   - **ref exists and subdir already contains a `cinna.agent.json`** → raise
     `GitSourceConflictError` ("this repo/subdir already holds an agent — use
     checkout to import it") → 409. (Prevents accidental overwrite of an existing
     agent tree; connect is for a *fresh* destination.)
9. **Capture + commit** (reuse exactly the `_push_locked` body):
   build manifest (`RevisionFormat.build_manifest` with `cred/schedule/plugin`
   specs from `PublishService._collect_*`), `RevisionFormat.write_tree(...,
   manifest_filename=GIT_MANIFEST_FILENAME)`, write `.gitignore`,
   `_assert_no_oversized_files`, `commit_all`, `fast_forward_push`.
10. **Persist `AgentGitSource`** (`status=CONNECTED`, `last_synced_commit=new_sha`,
    `last_sync_at=now`).
11. Error split identical to push: `GitSourceConflictError /
    GitSourceValidationError / GitSourceNotFoundError` re-raise unchanged
    (no row stamped — the row may not exist yet); genuine operational failures
    surface as 400/401 and **no half-state** is left (the source row is only
    written on success).

**Refactor note (allowed — uncommitted):** extract the capture+commit body shared
by `_push_locked` and `connect` into a private `_capture_and_push(session, *,
install, env, source_like, owner, key, repo, repo_path, commit_message, version,
revision_number_hint)` helper so the two paths cannot drift. `source_like` carries
`repo_url/subdir/ref/bundle_uuid` (for `connect` it is the in-memory unsaved
source; for push it is the persisted row).

### A.3 Route `POST /agents/{agent_id}/git/connect`

**File:** `backend/app/api/routes/agent_git.py` (modify)

```python
class AgentGitConnectRequest(SQLModel):
    repo_url: str
    subdir: str | None = None
    ref: str = "main"
    ssh_key_id: uuid.UUID | None = None
    sync_direction: str = GitSyncDirection.BIDIRECTIONAL
    commit_message: str = "Initial export from Cinna"

@router.post("/{agent_id}/git/connect",
             response_model=AgentGitSourcePublic,
             dependencies=[Depends(require_developer)])
async def connect_git_source(agent_id, request, session, current_user) -> AgentGitSourcePublic:
    ...
```

- Developer-gated (it creates a versioning link + pushes).
- `try/except` reuses the existing `_map_git_error` + `IntegrityError` race
  backstop (mirrors `checkout_agent`).
- Returns `AgentGitSourcePublic` (with `update_available=False` immediately after
  connect — we just pushed, so the remote == `last_synced_commit`).

### A.4 Disable / disconnect route

**File:** `backend/app/api/routes/agent_git.py` (modify)

```python
@router.delete("/{agent_id}/git",
               dependencies=[Depends(require_developer)])
def disconnect_git_source(agent_id, session, current_user) -> Message:
    ...  # GitSourceService.disconnect(session, agent_id, current_user)
```

- `GitSourceService.disconnect(session, agent_id, owner)`: `_resolve_source_owned`
  → `session.delete(source)` → commit. Returns a `Message`. **Does not touch the
  remote** (the external repo is the durable record; disconnect only severs the
  platform link). Reuses `Message` model.

### A.5 Phase A validation / tests

**File:** `backend/tests/api/agents/agents_git_source_test.py` (extend — already exists)

- connect on owned agent with running env → 201/200, source row `connected`,
  remote has one commit at `subdir`, `last_synced_commit` set.
- connect when a source already exists → 409.
- connect with stopped/missing env → 400.
- connect with `sync_direction="pull"` → 400.
- connect to an **empty** remote (init path) vs a remote with **prior unrelated
  history** (ff path) → both succeed; subdir-already-has-agent → 409.
- connect with foreign `ssh_key_id` (not owned) → 400 (validation).
- disconnect removes the row; second disconnect → 404.

> Client regen after routes change:
> `source ./backend/.venv/bin/activate && make gen-client`

---

## Phase B — Piece 3: Commit history + dirty check (thin endpoints)

### B.1 git_operations primitive `git_log_subdir`

**File:** `backend/app/services/knowledge/git_operations.py` (modify)

```python
def git_log_subdir(
    *, repo_url: str, ref: str = "main", subdir: str | None = None,
    ssh_key_path: str | None = None, max_count: int = 50,
) -> list[dict]:
    """Return up to `max_count` commits touching `subdir`, newest first.

    Each dict: {sha, short_sha, author_name, author_email, date (ISO-8601),
    message}. Egress-guarded. Bounded shallow clone (depth=max_count) into a
    temp dir, `git log --max-count=N -- <subdir>/`, temp dir removed in finally.
    """
```

- Uses `clone_repository_context(..., depth=max_count)` (shallow, bounded —
  tradeoff: history older than `max_count` commits is not visible; acceptable for
  a UI list — see Open Questions Q3).
- Parses `repo.git.log("--max-count", N, "--pretty=...", "--", f"{subdir}/")`
  with a `%x1f`/`%x1e` delimited format for safe field splitting.
- Errors map through the existing typed git errors (`GitAuthenticationError`,
  `GitConnectionError`, `GitOperationError`).

### B.2 Workspace-only digest helper

**File:** `backend/app/services/bundles/publish_service.py` (modify) — add a
small sibling to `_hash_tree_with_manifest`:

```python
@staticmethod
def hash_workspace_tree(workspace_root: Path) -> str:
    """SHA-256 over files under a `workspace/` subtree only (NO manifest).

    Stable across rebuilds (excludes revision_number/published_at), so two
    captures of identical files hash equal — the dirty-check primitive.
    """
```

- Same walk/sort/byte-feed as `_hash_tree_with_manifest` minus the manifest body.
  Rooted at the `workspace/` dir so relative paths match on both sides.

### B.3 Service method `GitSourceService.compute_dirty`

**File:** `backend/app/services/bundles/git_source_service.py` (modify)

```python
@staticmethod
def compute_dirty(session: Session, agent_id: uuid.UUID, owner: User) -> dict:
    """Compare the LIVE workspace + prompts against the last synced revision.

    Returns {dirty: bool, prompts_dirty: bool, workspace_dirty: bool,
    last_synced_commit, has_env: bool}. NEVER pushes. Best-effort: if no env or
    no last revision, returns dirty=False with reason flags.
    """
```

Logic:

1. `_resolve_source_owned` (404 for non-owner).
2. `prompts_dirty` = refactored boolean form of `_assert_not_dirty` (compare the
   four prompt fields against `install.installed_revision_id`). **Refactor**
   `_assert_not_dirty` to call a new `_prompts_dirty(session, install) -> bool`
   and raise on `True` — single source of truth so pull's guard and the dirty
   endpoint can never disagree.
3. `workspace_dirty`:
   - resolve env; if no env → `workspace_dirty=False`, `has_env=False`.
   - snapshot the live env workspace via `PublishService._snapshot_workspace_tree`
     into a temp dir (denylist + symlink guards), then
     `PublishService.hash_workspace_tree(temp/workspace)`.
   - resolve the last synced revision (`install.installed_revision_id` or the
     bundle's latest git revision) and
     `hash_workspace_tree(Path(revision.snapshot_path)/"workspace")`.
   - `workspace_dirty = live_digest != synced_digest`.
   - temp dir removed in `finally`.
4. `dirty = prompts_dirty or workspace_dirty`.

> This is read-only and the same work push does **minus** the network and the
> commit, so it is safe to call on every card render / poll. (Cost note: it does
> a full workspace tree copy to temp; gate the FE to refetch on focus / explicit
> refresh rather than a tight interval — see Open Questions Q3.)

### B.4 Routes

**File:** `backend/app/api/routes/agent_git.py` (modify)

```python
class GitCommit(SQLModel):
    sha: str; short_sha: str; author_name: str; author_email: str
    date: datetime; message: str

class GitCommitList(SQLModel):
    commits: list[GitCommit]

class GitDirtyStatus(SQLModel):
    dirty: bool; prompts_dirty: bool; workspace_dirty: bool
    has_env: bool; last_synced_commit: str | None = None

@router.get("/{agent_id}/git/commits", response_model=GitCommitList)
def list_git_commits(agent_id, session, current_user, limit: int = 50): ...

@router.get("/{agent_id}/git/dirty", response_model=GitDirtyStatus)
def get_git_dirty(agent_id, session, current_user): ...
```

- Both **owner-resolved reads** (no developer gate — consistent with
  `GET /git` and `/git/check-updates`).
- `list_git_commits` is **strict** (surfaces auth/network errors like
  `check-updates`); `get_git_dirty` is best-effort on the env/remote side but
  may 404 if no source. Error mapping via existing `_map_git_error`.
- `limit` clamped server-side to e.g. `1..200`.

> **Decision (folding):** keep `dirty` as its **own** endpoint rather than folding
> into `GET /git`. `GET /git` is a cheap, never-failing status read; the dirty
> check does a workspace tree copy and must not slow/again-fail that read.

### B.5 Phase B validation / tests

- commits list after connect → exactly one commit with the connect message;
  after a push → grows; scoped to `subdir` (a commit touching only another
  subdir does not appear).
- dirty=False immediately after connect/push; edit a workspace file in the env →
  `workspace_dirty=True`; change a prompt field → `prompts_dirty=True`;
  no-env → `has_env=False, dirty=False`.
- non-owner → 404 on both.

> Client regen after routes change.

---

## Phase C — Piece 4: cinna-cli sparse-checkout contract (cinna-core side)

The CLI repo is **separate** (`/Users/evgenyl/dev/ml-llm/cinna-cli`) and is **not
modified by this effort**. cinna-core ships only: (a) a CLI-reachable discovery
endpoint exposing repo coordinates **without** the deploy key, and (b) the written
CLI-side plan (Section "Piece 4 — cinna-cli plan", below).

### C.1 CLI coordinates endpoint

**File:** `backend/app/api/routes/cli.py` (modify; router prefix `/cli`, mounted
at `/api/v1` ⇒ full path `/api/v1/cli/git-coordinates`)

```python
class CliGitCoordinates(SQLModel):
    vcs_enabled: bool
    repo_url: str | None = None      # external remote (NOT a secret)
    subdir: str | None = None
    ref: str | None = None
    sync_direction: str | None = None
    last_synced_commit: str | None = None
    auth_hint: str | None = None     # "ssh" | "https" | None — how the USER should auth
    # NEVER includes ssh_key material.

@router.get("/git-coordinates", response_model=CliGitCoordinates)
def cli_git_coordinates(cli_ctx: CLIContextDep, db: SessionDep) -> CliGitCoordinates:
    """Tell the CLI whether this agent is VCS-enabled and where the remote is.

    Auth: CLI token scoped to exactly one agent (cli_ctx.agent). No deploy key,
    no private-key material — the developer uses their OWN git/SSH client.
    """
```

- Resolution: `select(AgentGitSource).where(agent_id == cli_ctx.agent.id)`.
  - none → `vcs_enabled=False` (all other fields `None`).
  - present → `vcs_enabled=True` + coordinates. `auth_hint` derived from
    `repo_url` shape (`git@…`/`ssh://` ⇒ `"ssh"`, `https://` ⇒ `"https"`) so the
    CLI can tell the user which credentials they need locally.
- **Security:** `repo_url`/`subdir`/`ref` are not secrets (the agent owner already
  sees them in the UI). The deploy key (`ssh_key_id`) is **never** referenced in
  the response model. The CLI token is already agent-scoped, so no extra
  ownership check beyond the existing `cli_ctx`.
- No developer-role gate (CLI tokens are owner-minted and agent-scoped; matches
  the rest of the CLI surface).

### C.2 Phase C validation / tests

**File:** `backend/tests/api/agents/` (CLI-context test, follow existing CLI test
patterns) — or extend `agents_git_source_test.py` with a CLI-token fixture.

- VCS-enabled agent → `vcs_enabled=True`, coordinates correct, **no key field**
  present in the serialized response.
- non-VCS agent → `vcs_enabled=False`, coordinates `None`.
- CLI token scoped to agent A cannot read agent B's coordinates (scope enforced
  by `cli_ctx.agent`).

> Client regen after routes change (CLI also consumes the generated client, but
> the cinna-cli repo regenerates independently — note in the CLI plan).

---

## Phase D — Piece 2: "GIT Versioning" Integrations card (frontend)

This is the **first UI** for the entire feature. It wires enable/disable, connect,
push ("Commit Agent"), pull/update-available, dirty state, and commit history.

### D.1 Client regeneration (prerequisite)

After Phases A–C land, run
`source ./backend/.venv/bin/activate && make gen-client`. This adds to
`AgentGitService` (tag `agent-git`): `connectGitSource`, `disconnectGitSource`,
`listGitCommits`, `getGitDirty` (plus the existing `getGitSource`,
`checkGitUpdates`, `pullGitSource`, `pushGitSource`, `checkoutAgent`). `SshKeysService`
already exists for the deploy-key picker.

### D.2 New component `GitVersioningCard`

**File:** `frontend/src/components/Agents/GitVersioningCard.tsx` (new)

**Placement:** `frontend/src/components/Agents/AgentIntegrationsTab.tsx` (modify)
— add `<GitVersioningCard agentId={agent.id} agentName={agent.name} />` to the
card grid alongside `LocalDevCard`, `AgentRestApiCard`, `McpConnectorsCard`.
**Owner-gated** like `AgentWebhooksCard` (`isOwner` / not `isAgentUser`); the card
is developer-only in effect because connect/push/pull are developer-gated.

**States:**

1. **Disabled (no source)** — header toggle OFF (default). A short description
   ("Version this agent's workspace in an external git repo"). Toggling ON reveals
   the **connect form**.
2. **Connect form** (toggle ON, not yet connected):
   - `repo_url` (Input), `subdir` (Input, optional), `ref` (Input, default
     `main`), `sync_direction` (Select: bidirectional/push/pull).
   - **Deploy key picker** (`DeployKeySelect`, see D.3).
   - `commit_message` (Input, default "Initial export from Cinna").
   - **Connect** button → `AgentGitService.connectGitSource`. On success, refetch
     `["git-source", agentId]`; toast.
3. **Connected** — show:
   - repo/subdir/ref + status badge (`connected`/`error` with `last_error`).
   - **Update banner** when `update_available` (from `getGitSource` /
     `checkGitUpdates`) → **Pull** button (`pullGitSource`).
   - **Commit Agent** button — **enabled only when `getGitDirty().dirty`** is
     true (else disabled with "No local changes"). Opens a small dialog for the
     commit message → `pushGitSource`.
   - **Commit history** list from `listGitCommits` (short_sha, message, author,
     relative date). Paginate via `limit` or a "show more".
   - **Disconnect** (in an overflow menu or footer) → confirm dialog →
     `disconnectGitSource`; resets the card to disabled.

### D.3 Deploy key picker / quick-generate (reuse ssh_keys)

**File:** `frontend/src/components/Agents/DeployKeySelect.tsx` (new)

- Lists the user's SSH keys via `SshKeysService.listSshKeys` (query key
  `["sshKeys"]` — already used by Settings).
- Options: **(a)** pick an existing key, **(b)** "None (public repo)",
  **(c)** "Generate a new key…".
- **Quick-generate:** inline reuse of the existing generate flow
  (`SshKeysService.generateSshKey`, the same call `GenerateKeyModal` makes).
  On success, show the **public key** in a copy-able block with deploy-key
  guidance:
  > "Add this as a **Deploy key** in your GitHub/GitLab repo settings and
  > **check 'Allow write access'** so the platform can push." 
  Then auto-select the new key. Invalidate `["sshKeys"]`.
- The picker returns the chosen `ssh_key_id | null` to the connect form. **No
  private-key material is ever displayed** (consistent with the ssh_keys
  feature — only the public key is shown).

> Consider extracting the generate-success view from
> `frontend/src/components/UserSettings/GenerateKeyModal.tsx` into a shared
> snippet to avoid duplicating the public-key + copy UI (optional cleanup).

### D.4 React Query hooks (in-component or `frontend/src/hooks/useGitVersioning.ts`)

| Hook | Service call | Query key | Notes |
|---|---|---|---|
| source status | `AgentGitService.getGitSource` | `["git-source", agentId]` | drives connected/disabled + `update_available` |
| dirty | `AgentGitService.getGitDirty` | `["git-dirty", agentId]` | `refetchOnWindowFocus`; gates "Commit Agent"; **not** a tight interval |
| commits | `AgentGitService.listGitCommits` | `["git-commits", agentId, limit]` | enabled only when connected |
| connect | `AgentGitService.connectGitSource` | — | invalidates `git-source`, `git-commits`, `git-dirty` |
| push | `AgentGitService.pushGitSource` | — | invalidates `git-commits`, `git-dirty`, `git-source` |
| pull | `AgentGitService.pullGitSource` | — | invalidates all three + `["agent", agentId]` |
| disconnect | `AgentGitService.disconnectGitSource` | — | invalidates `git-source` → card returns to disabled |
| ssh keys | `SshKeysService.listSshKeys` / `generateSshKey` | `["sshKeys"]` | deploy-key picker |

- `getGitSource` is best-effort for `update_available`; on a 404 the card shows
  the disabled/connect state (no source yet).
- Error toasts via `useCustomToast` (mirror `AgentIntegrationsTab` patterns).

### D.5 Phase D validation

- `npx tsc --noEmit 2>&1 | grep -E "GitVersioningCard|DeployKeySelect|AgentIntegrationsTab"`
  (per CLAUDE.md — scoped typecheck).
- Manual: connect a public repo (no key), connect a private repo (generate key →
  add as deploy key → connect), edit a file in the env → "Commit Agent" lights up
  → push → commit appears in history → advance remote externally → update banner →
  pull. Disconnect returns the card to disabled.

---

## Piece 4 — cinna-cli plan (written; implemented in the separate repo)

> **Repo:** `/Users/evgenyl/dev/ml-llm/cinna-cli`. **Not touched by this effort.**
> This section is the precise contract + command design the CLI work will follow.

### Backend contract the CLI consumes

| Method | Path | Auth | Returns | Purpose |
|---|---|---|---|---|
| `GET` | `/api/v1/cli/git-coordinates` | CLI token (agent-scoped) | `CliGitCoordinates` | Discover `vcs_enabled` + `{repo_url, subdir, ref, sync_direction, last_synced_commit, auth_hint}`. **No deploy key.** |

Everything else the CLI needs (initial file copy, exec, status) already exists in
the CLI integration surface. The **deploy key never leaves the backend**; on the
local machine the developer authenticates to the remote with their **own**
git/SSH client and their **own** GitHub/GitLab access.

### Local-git linking command sequence (sparse-checkout, one repo ↔ many agents)

Triggered during `cinna setup` / `cinna dev` when
`GET /cli/git-coordinates → vcs_enabled=true` **and** the local folder has no
`.git`:

1. **Copy live files first** (existing initial-workspace fetch) into the agent
   folder — these may contain the backend's **uncommitted** changes (e.g. the
   working tree at connect time). Keep them in place; do **not** discard.
2. `git init` in the agent folder; `git remote add origin <repo_url>`.
3. `git fetch --depth=1 origin <ref>` (developer's own credentials).
4. `git sparse-checkout init --cone` (or non-cone for arbitrary subdirs);
   `git sparse-checkout set <subdir>` — so one local repo can host **many** agent
   folders, each its own subdir of the same remote.
5. `git checkout <ref>` then **`git reset --mixed origin/<ref>`** (not
   `--hard`) — this points HEAD at the remote tree **without** overwriting the
   already-copied live files. Result: the backend's uncommitted changes now show
   up as **local uncommitted changes** in `git status`, which the developer
   reviews and **commits/pushes himself** with their own creds.
   - If the remote `subdir` is empty/absent (agent connected to an empty remote
     and not yet pushed by backend), step 5 degrades to "untracked files" — same
     net effect: the user commits the initial tree.
6. **Teach the coding session it is git-tracked:** append a short block to the
   generated `CLAUDE.md` / `AGENTS.md` ("This workspace is a git working tree
   tracking `<repo_url>` subdir `<subdir>` on `<ref>`. Commit and push with your
   own git client; the platform also pushes via a deploy key — both are
   fast-forward-only.").

### Two-writer model (Decision D)

- Backend pushes via deploy key (`GitSourceService.push`/`connect`,
  fast-forward-only). Developer pushes via own creds (plain `git push`).
- **Both** target the same external remote/ref. Both are fast-forward-only:
  - backend ff is enforced by `fast_forward_push` (409 "pull first" on divergence);
  - developer ff is enforced by normal git (`! [rejected] non-fast-forward`).
- Conflict resolution is **fail-loud** on both sides and reconciled by the human
  via standard git (`git pull --rebase` locally; "pull first" in the UI for the
  backend). No auto-merge.

### Detect-and-instruct (never auto-convert)

On `cinna dev` / `cinna status`, after `GET /cli/git-coordinates`:

- `vcs_enabled=true` **and** local folder **has `.git`** → normal git-tracked dev.
- `vcs_enabled=true` **and** local folder has **no `.git`** (local checkout
  predated VCS enablement) → **warn, do not convert**:
  > "This agent is now git-versioned, but this local folder isn't a git working
  > tree. Run `cinna disconnect` here and re-sync (`cinna setup` / `cinna dev`)
  > to get git support."
  The CLI must **not** silently `git init` an existing Mutagen folder (avoids
  surprising the user / clobbering state).
- `vcs_enabled=false` → unchanged Mutagen-only behavior.

### CLI-side test/validation notes (for the cinna-cli repo)

- coordinates parsing (ssh vs https `auth_hint`); `vcs_enabled=false` path is a
  no-op.
- link sequence on empty remote vs populated subdir; verify `git status` shows
  the backend's uncommitted changes as local changes after `reset --mixed`.
- detect-and-instruct: existing non-`.git` Mutagen folder triggers the warning and
  changes nothing on disk.
- CLI regenerates its own client against the updated OpenAPI (separate repo step).

---

## Security invariants preserved (carried from the foundation)

- **Deploy key host-side only.** `_resolve_ssh_key` → `get_decrypted_private_key`
  (ownership-checked) → chmod-600 temp file → deleted in `finally`. The key never
  reaches the container, is never logged, never appears in any Public model — and
  the new `CliGitCoordinates` model has **no key field**.
- **Egress/SSRF guard on every network call.** `connect`, `init_repo_with_remote`,
  `git_log_subdir`, and every `ls_remote_head`/clone go through
  `assert_git_url_allowed`.
- **Denylist + `.gitignore` derivation unchanged.** Initial-export push reuses
  `RevisionFormat.write_tree` (→ `_snapshot_workspace_tree` + symlink guards) and
  `generate_gitignore`, so credentials/app-data/logs/databases/uploads can never
  be committed — including the dirty check's temp snapshot (same copy path).
- **Per-agent lock** wraps `connect` (mirrors push/pull) so connect cannot race a
  concurrent push on the same agent.
- **Ownership / no existence leak.** `connect`/`disconnect`/`commits`/`dirty`
  resolve via the existing `_resolve_source_owned` / owned-install pattern (404 for
  non-owners). `cli_git_coordinates` is bounded by the agent-scoped CLI token.
- **`required_credential_specs` metadata only** — initial push uses the same
  `_collect_credential_specs` + `_template_payload_for` stripping as publish.

---

## Reuse vs. extend summary

| Concern | Reuse as-is | Extend (uncommitted ⇒ free to modify) | New |
|---|---|---|---|
| Capture + commit + ff-push | `_push_locked` body | extract `_capture_and_push` shared helper | — |
| Bundle resolution | `_resolve_or_create_bundle`, `_persist_revision` | — | — |
| Dirty (prompts) | — | `_assert_not_dirty` → `_prompts_dirty` boolean | — |
| Dirty (workspace) | `_snapshot_workspace_tree` | — | `PublishService.hash_workspace_tree`, `GitSourceService.compute_dirty` |
| Commit history | `clone_repository_context` | — | `git_log_subdir` |
| Empty-remote bootstrap | `commit_all`, `fast_forward_push` | `fast_forward_push` (handle absent remote ref) | `init_repo_with_remote` |
| SSH deploy key | `_resolve_ssh_key`, `SSHKeyService`, `SshKeysService` (FE) | — | `DeployKeySelect` (FE) |
| Routes | `_map_git_error`, error split | `agent_git.py` (add connect/disconnect/commits/dirty) | — |
| CLI discovery | `CLIContextDep`, `cli_ctx.agent` | `cli.py` (add endpoint) | `CliGitCoordinates` |
| Card host | `AgentIntegrationsTab` grid | mount card | `GitVersioningCard`, hooks |

---

## Open Questions / Decisions

- **Q1 — Disable = delete vs soft-disconnect?** This plan deletes the
  `AgentGitSource` row on disconnect (clean re-enable, keeps the unique
  `agent_id` index meaningful, no migration). Alternative: set
  `status=DISCONNECTED` and keep the row (preserves `last_synced_commit`
  history). **Proposed: delete.** Revisit if we want disconnect→reconnect to
  remember the last commit without re-probing the remote.
- **Q2 — Connect on an unlinked agent: mutate `Agent.bundle_uuid`?** Proposed:
  **no** — give the `AgentGitSource` its own `bundle_uuid` (resolve/create keyed
  on `agent.bundle_id`) and leave `Agent.bundle_uuid` NULL, so publisher/consumer
  semantics (`Agent` line ~217) are untouched. Confirm this doesn't strand the
  later publish flow (publish resolves its own bundle by `bundle_id`; an existing
  ownerless row keyed on the same id would be reused — acceptable, but verify the
  publish path's `_resolve_or_create_bundle` equivalent agrees).
- **Q3 — Commit history depth & dirty-check cost.** `git_log_subdir` uses a
  bounded shallow clone (`depth=max_count`), so very old history is invisible in
  the list. The dirty check copies the whole workspace to a temp dir per call —
  fine on demand/focus, but the FE must avoid a tight polling interval. Confirm
  `max_count` default (50) and the FE refetch strategy.
- **Q4 — Subdir-already-has-agent on connect.** Proposed 409 ("use checkout").
  Alternative: allow connect to *adopt* an existing remote tree (becomes a
  checkout-like import). Kept out of scope to preserve Decision C's clean split.
- **Q5 — `also_publish_bundle` on connect?** Push has it; connect omits it
  (initial export shouldn't implicitly publish a catalog bundle). Confirm.
- **Q6 — Empty-remote first push & `fast_forward_push`.** Verify the merge-base
  check treats an absent remote ref as "ancestor-OK"; if not, add an explicit
  first-push branch (allowed — `git_operations.py` is uncommitted).

---

## Client-regen reminder

Run after **every** phase that changes routes (A, B, C):

```bash
source ./backend/.venv/bin/activate && make gen-client
```

This regenerates `frontend/src/client/{sdk,types,schemas}.gen.ts`
(`AgentGitService`, `CliGitCoordinates`, new request/response types) consumed by
Phase D. The cinna-cli repo regenerates its own client separately.
