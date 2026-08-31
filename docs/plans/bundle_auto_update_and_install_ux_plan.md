# Bundle Auto-Update Convergence + Install/Admin Update UX — Implementation Plan

**Status:** ready to implement
**Feature area:** `agent_bundles` (see `docs/agents/agent_bundles/agent_bundles.md`), `admin_agent_environments`
**Date:** 2026-08-07

---

## 1. Problem

A consumer install with `update_mode='automatic'` and a pending revision never updates when its
environment is already suspended.

Confirmed against production data:

| install | mode | pending | pending_at | installed rev | env status | last_activity |
|---|---|---|---|---|---|---|
| `59251d89` | automatic | false | — | `300da62d` (latest) | suspended | 15:37:12 |
| `593a024d` | automatic | **true** | 15:35:53 | `dd259aec` (older) | **suspended** | *(null)* |

### Root cause

`InstallService.apply_update` has exactly three callers:

- `POST /agents/{id}/apply-update` (`backend/app/api/routes/installs.py:143`) — owner-triggered
- `POST /external/installs/{id}/apply-update` (`backend/app/api/routes/external_agents.py:147`) — owner-triggered
- `backend/app/services/environments/environment_suspension_scheduler.py:139-156` — the only automatic path

That third one lives **inside** the `if should_suspend:` branch of a loop whose source query is:

```python
statement = select(AgentEnvironment).where(AgentEnvironment.status == "running")  # :52
```

So auto-apply is a **running → suspended transition hook**, not a "this install is behind" sweep. An
environment that was already suspended when the revision was published is excluded by the very first
filter and is never revisited. `PublishService.notify_installs`
(`backend/app/services/bundles/publish_service.py:384-428`) only sets `pending_update=True`; it applies
nothing. Neither `activate_suspended_environment` nor `usage_intent.py` consults `pending_update` on
resume. Result: for an install whose owner is not actively using it, "automatic" means "never".

Three further paths into a non-running state bypass the hook entirely:
`EnvironmentService.suspend_environment` (`environment_service.py:1095`, the manual Suspend button),
`sync_activity_tracker.py:203` (CLI sync cooldown), and any stop/error transition.

### Secondary problem

A consumer cannot opt into automatic mode after installing. `Agent.update_mode` is stamped once at
install time from `bundle.default_install_mode` (`install_service.py:258`); `_apply_revision_metadata`
(`install_service.py:189-221`) deliberately excludes `update_mode`, so publishing never pushes it. The
`PATCH /agents/{id}/update-mode` endpoint exists and is reachable by any install owner, but **no
frontend component calls it**.

> Note: the comment block at `installs.py:63-66` claims update-mode is developer-only. It is not — the
> decorator at `installs.py:167` has no `require_developer` dependency, only `_resolve_install_owned`.
> Fix the stale comment as part of this work; do **not** add a role gate.

---

## 2. Locked decisions

| # | Decision |
|---|---|
| D1 | New **dedicated scheduler module**, not another branch in the suspension job. |
| D2 | Keep the existing suspension-time hook exactly as-is (best moment for a running env; removing it would force an unnecessary stop/start cycle). |
| D3 | Sweep selects on **revision mismatch**, not `pending_update` — self-healing if an event was lost. |
| D4 | Environment gating is an **explicit allowlist**: `active_environment_id IS NULL`, or status in `{suspended, stopped}`. Everything else — including `error` and every transitional status — is skipped. |
| D5 | **Publish-time fast path**: `notify_installs` additionally fires the same sweep scoped to that bundle, as a background task, so publish stays fast. |
| D6 | Both entry points call **one shared function** — a single implementation of selection + gating. |
| D7 | **Single-leader** via `pg_try_advisory_lock`, mirroring `model_discovery_scheduler.py:53-69`. |
| D8 | **Failure backoff** via a new `Agent.last_update_attempt_at` column + migration. |
| D9 | **Bundle Installation card on foreign installs only** (`bundle_uuid && !is_publisher_install`). |
| D10 | **"Update now" stays visible in automatic mode too**; correct `agent_bundles.md:107` which claims otherwise. |
| D11 | **No retroactivity**: flipping the bundle dropdown affects new installs only. Existing installs keep their `update_mode`. Nothing propagates on publish. |
| D12 | Admin envs gets both the **Bundle column** and an **`update_available` filter**. |

---

## 3. Phase 1 — Backend: auto-update convergence

### 3.1 Migration

Add one nullable column:

```python
Agent.last_update_attempt_at: datetime | None = Field(default=None)
```

Model: `backend/app/models/agents/agent.py` (the `Agent` table class, near
`last_sync_at` / `last_update_status` ~line 196-198). Not exposed on `AgentPublic` unless a UI needs it.

> The repo has pre-existing multiple alembic heads. Generate the migration, hand-check it for autogen
> drift (it must contain **only** this column), and set `down_revision` to a clean single child. Do not
> attempt to merge unrelated heads in this work.

### 3.2 Shared sweep function

`backend/app/services/bundles/install_service.py`:

```python
@staticmethod
async def sweep_automatic_updates(
    session: Session,
    *,
    bundle: AgentBundle | None = None,
    limit: int = 50,
) -> dict:
    """Apply pending revisions to automatic-mode installs whose env is not live.

    Returns {"applied": int, "skipped": int, "failed": int, "deferred": int}.
    """
```

**Selection** (single query, joined — no per-row `session.get`):

```sql
agent.bundle_uuid = agent_bundle.id
AND agent.is_publisher_install = false
AND agent.update_mode = 'automatic'
AND agent.installed_revision_id IS DISTINCT FROM agent_bundle.latest_revision_id
AND agent_bundle.latest_revision_id IS NOT NULL
-- when bundle is not None: AND agent_bundle.id = :bundle_id
```

Backoff filter (D8): skip rows where `last_update_status = 'failed'` **and**
`last_update_attempt_at > now() - AUTO_UPDATE_RETRY_BACKOFF` (config, default 6 hours). Count these as
`deferred`.

**Env gating (D4)** — LEFT JOIN `agent_environment` on `Agent.active_environment_id`:

- `active_environment_id IS NULL` → apply (DB-only path; `apply_update` skips `replace_bundle_content`
  when `env is None`)
- `env.status IN ('suspended', 'stopped')` → apply
- anything else → skip, count it, `logger.debug` the reason. Never apply to `running`, `error`, or any
  transitional status (`creating`, `building`, `initializing`, `starting`, `rebuilding`, `activating`).

**Per-install execution:**

1. `SELECT ... FOR UPDATE` the `agent_environment` row (when there is one) and **re-read `status`**;
   bail out of this install if it left the allowlist between the batch query and now. This shrinks the
   activation race to the copy itself without introducing a new env status value.
2. Stamp `install.last_update_attempt_at = now()` and commit **before** calling `apply_update`, so a
   crash mid-apply still records the attempt.
3. `await InstallService.apply_update(session, install)`.
4. Wrap each install in its own `try/except` — one failure must not abort the batch. `apply_update`
   already sets `last_update_status="failed"` and emits `INSTALL_UPDATE_FAILED`
   (`install_service.py:1363-1380`); the sweep only logs and counts.
5. Stop at `limit`. If more rows matched than were processed, `logger.info` the remainder explicitly —
   no silent truncation.

**Why this is safe on a suspended env:** `apply_update` computes `was_running = env.status == "running"`
(`install_service.py:1239`); when false it skips both the stop and the restart.
`replace_bundle_content` (`backend/app/services/environments/workspace_copy.py:252-290`) is a pure
host-filesystem operation on `ENV_INSTANCES_DIR/<env_id>` and returns early if the directory is missing.
The env stays suspended with new content on disk, and the nulled prompt-sync baselines
(`install_service.py:1257-1259`) make the next activation SEED_PUSH from the DB.

### 3.3 Scheduler module

New: `backend/app/services/environments/bundle_auto_update_scheduler.py`

Follow the established shape (`environment_suspension_scheduler.py`, `model_discovery_scheduler.py`):
module-level `BackgroundScheduler`, sync `run_*` entry that wraps `asyncio.run(...)`, `start_scheduler()`
/ `shutdown_scheduler()`.

- Interval: 10 minutes (config `BUNDLE_AUTO_UPDATE_INTERVAL_MINUTES`, default 10).
- **Single-leader (D7):** acquire `pg_try_advisory_lock` on the batch session with a new unique key
  constant; skip the run when not acquired; release in `finally` on the same session/connection. Copy
  the pattern from `model_discovery_scheduler.py:53-69` verbatim in structure.
- Register in `backend/app/main.py` inside the existing `if not settings.TESTING:` block
  (`main.py:166-180`) and add the matching shutdown call alongside `shutdown_suspension_scheduler()`
  (`main.py:359`).

> Do **not** add the advisory lock to `environment_suspension_scheduler.py` in this work. It has the same
> multi-worker flaw, but that is a separate fix with its own blast radius.

### 3.4 Publish-time fast path (D5)

In `PublishService.notify_installs` (`publish_service.py:384-428`), after the existing
`pending_update` marking and event fan-out, schedule the bundle-scoped sweep as a background task using
`create_task_with_error_logging` (`app/utils.py`) with a **fresh session** — do not reuse the request
session inside a detached task. Publish must return immediately; a sweep failure must never fail a
publish.

### 3.5 Config

`backend/app/core/config.py`:

- `BUNDLE_AUTO_UPDATE_ENABLED: bool = True`
- `BUNDLE_AUTO_UPDATE_INTERVAL_MINUTES: int = 10`
- `BUNDLE_AUTO_UPDATE_BATCH_LIMIT: int = 50`
- `BUNDLE_AUTO_UPDATE_RETRY_BACKOFF_HOURS: int = 6`

---

## 4. Phase 2 — Backend: check-updates enrichment

`CheckUpdatesResponse` (`backend/app/models/bundles/catalog.py`) and
`InstallService.check_for_updates` (`install_service.py:1418-1466`) gain two additive fields, both read
straight off the resolved latest revision row:

- `latest_release_notes: str | None`
- `latest_published_at: datetime | None`

No migration. Existing consumers are unaffected.

---

## 5. Phase 3 — Backend: admin envs enrichment

`AdminAgentEnvironmentPublic` (`backend/app/models/environments/environment.py:240-266`) gains:

```python
bundle_id: str | None = None                    # reverse-DNS string from Agent.bundle_id
is_publisher_install: bool = False
update_mode: str | None = None
installed_revision_number: int | None = None
installed_revision_version: str | None = None
latest_revision_number: int | None = None
latest_revision_version: str | None = None
update_available: bool = False
```

`AdminEnvironmentService.list_environments`
(`backend/app/services/environments/admin_environment_service.py:84+`):

- extend the existing single env → agent → user join (`:120-124`) with a LEFT JOIN to `agent_bundle` on
  `Agent.bundle_uuid`
- collect every referenced revision id (installed + latest) across the page and resolve them in **one
  batched `IN` query** cached in a dict — mirror the `_tag_cache` approach at `:151-154`.
  **Do not `session.get` per row**: this list is fleet-wide and per-row lookups are exactly the N+1 that
  already bit the model-health rollup.
- `update_available = bundle is not None and latest_revision_id is not None and
  installed_revision_id != latest_revision_id and not is_publisher_install`

New query param on `GET /admin/agent-environments/`
(`backend/app/api/routes/admin_environments.py:41-69`): `update_available: Optional[bool]`, applied
post-enrichment alongside the existing `is_stale` / `in_use` filters.

---

## 6. Phase 4 — Frontend

Regenerate the client first: `source ./backend/.venv/bin/activate && make gen-client`.

### 6.1 `BundleInstallationCard` (new)

`frontend/src/components/Agents/BundleInstallationCard.tsx`, mounted in
`frontend/src/components/Agents/AgentConfigTab.tsx`.

**Visibility (D9):** render only when `agent.bundle_uuid && !agent.is_publisher_install`. Returns `null`
otherwise.

**Critical:** `AgentConfigTab` receives `readOnly=true` for every foreign install
(`frontend/src/routes/_layout/agent/$agentId.tsx:235`) because bundle content is publisher-authored.
This card is **exempt** — update mode is the consumer's own preference, not publisher content. Do not
thread `readOnly` into it.

Contents:

- **Bundle ID** — `agent.bundle_id`, mono, truncating.
- **Installed version** — `v{installed_revision_version || installed_revision_number}` (both already on
  `AgentPublic`).
- **Latest available** — from `InstallsService.checkUpdates`. Unlike `UpdateAvailableBanner`, which
  queries only when `pending_update` is already true (`UpdateAvailableBanner.tsx:39`), this card queries
  **unconditionally** so it can render an explicit "Up to date" state. Show `latest_release_notes` and
  `latest_published_at` when present.
- **Update mode selector** — manual / automatic → `InstallsService.setUpdateMode`. Copy must state the
  real behavior: *"Automatic — applied while the agent is idle, usually within ~10 minutes of a new
  release."*
- **Update now** button whenever a newer revision exists — **visible in automatic mode too (D10)**.
  Calls `InstallsService.applyUpdate`; on success invalidate `["agent", agent.id]` and
  `["agent", agent.id, "check-updates"]`.

Keep `UpdateAvailableBanner` as-is — it stays the page-level attention grabber; the card is the durable
home for the same information. Reuse its `revisionLabel` helper (extract to a shared module rather than
duplicating).

### 6.2 Admin envs Bundle column

`frontend/src/components/Admin/Environments/AdminEnvTable.tsx` — new
`columnHelper.accessor("bundle_id", { header: "Bundle", ... })`, placed **immediately after the "Agent"
column** (`:235-247`); the table is already wide.

Cell:
- primary line: bundle ID, mono, truncated (`max-w-[160px]`, matching neighbours)
- secondary: installed version badge (`v1.4`)
- when `update_available`: amber `→ v1.5` badge, visually consistent with the existing `StaleBadge` /
  `ModelHealthCell` language — and clearly **distinct from image-tag staleness**, which is a different
  axis (rebuild vs. bundle revision)
- publisher installs: bundle ID only, never an update badge
- non-bundle agents: em dash

`frontend/src/components/Admin/Environments/AdminEnvFiltersBar.tsx` — add an `update_available` control
following the existing `is_stale` / `in_use` filter pattern, wired through
`frontend/src/routes/_layout/admin/agent-envs.tsx`.

---

## 7. Tests

Read `backend/tests/README.md` first (API-only, no direct DB access, scenario-based, shared utils).
Check for a domain README in the target directory.

**Run only the new tests — do not run the full suite.**

New coverage, in the bundles test area:

1. Suspended env + automatic + behind revision → sweep applies; `installed_revision_id` advances,
   `pending_update` clears, env **stays suspended** (not started).
2. Install with **no environment** + automatic + behind → applied (DB-only).
3. `stopped` env → applied.
4. `running` env → **not** touched by the sweep.
5. Transitional status (`starting` / `building`) → **not** touched.
6. `update_mode='manual'` + behind → untouched.
7. `is_publisher_install=true` → untouched.
8. Failure backoff: `last_update_status='failed'` with a recent `last_update_attempt_at` → deferred, not
   retried.
9. Publish fast path: publishing a new revision converges an already-suspended automatic install.
10. `check_for_updates` returns the new `latest_release_notes` / `latest_published_at` fields.
11. Admin env list returns the new bundle fields, `update_available` is correct for
    behind/current/publisher/non-bundle rows, and the `update_available` filter narrows correctly.

Frontend: typecheck the touched files only —
`cd frontend && npx tsc --noEmit 2>&1 | grep -E "(BundleInstallationCard|AgentConfigTab|AdminEnvTable|AdminEnvFiltersBar)"`.

---

## 8. Docs to update

- `docs/agents/agent_bundles/agent_bundles.md` — line 17 (update-mode definition), line 72 (publish
  flow: automatic installs now converge via fast path + sweep), line 107 (**correct the claim that
  manual-surface buttons are hidden for automatic installs** — D10), and document that the dropdown is
  new-installs-only (D11) with the per-install override now available in the UI.
- `docs/agents/agent_bundles/agent_bundles_tech.md` — new scheduler module, `sweep_automatic_updates`,
  the `last_update_attempt_at` column, extended `CheckUpdatesResponse`, new config keys.
- `docs/application/admin_agent_environments/*` — new row fields, Bundle column, `update_available`
  filter.
- `docs/README.md` — refresh the `agent_bundles` and `admin_agent_environments` registry descriptions.

---

## 9. Explicitly out of scope

- Adding the advisory lock to `environment_suspension_scheduler.py` (same flaw, separate fix).
- Any retroactive propagation of `update_mode` to existing installs (D11).
- A new `updating` environment status (revisit only if the `FOR UPDATE` + re-check proves insufficient).
- Merging the pre-existing multiple alembic heads.
