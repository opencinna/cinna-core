# Agent Addons — Technical Reference

Business logic: [agent_addons.md](agent_addons.md)

This feature adds **no table of its own**. It adds one derived projection, one
grant table on the skills catalog, two columns on the marketplace-entry table,
and a container-side manifest normaliser. Everything else is existing plumbing
addressed through one new list.

---

## Architecture

```
GET  /agents/{id}/addons          ─┐
POST /agents/{id}/addons/refresh  ─┤ routes/agent_skills.py
                                   │
                                   ▼
                       AddonsService.build(session, agent, user)
                          ├─ LLMPluginService.get_agent_plugins(session, agent.id)
                          │     → AgentPluginLinkWithUpdateInfo[]   (marketplace | bundle | catalog)
                          └─ AgentSkillsService.get_cached_entries(environment)
                                → SkillEntry[]  (source = local | plugin | catalog)
                                   │
                          fold on <marketplace>/<plugin>
                                   │
                                   ▼
                             AgentAddonsPublic
```

`build` performs **no writes and no container calls** — it reads the plugin links
and the environment's cached skill index. `refresh` is the one variant that
touches the environment: it calls `AgentSkillsService.force_refresh`, re-reads the
row, then builds. Every mutation stays on the existing plugin and skills routes.

---

## Wire shapes — `backend/app/models/agents/addons.py`

No tables. `AddonSkillPublic` is an **alias** of `SkillEntryPublic`, not a second
shape — a skill does not become a different thing because it is rendered inside a
plugin, and a second shape would be a second place to keep the status vocabulary
correct.

### `AddonPublic`

| Field | Type | Notes |
|-------|------|-------|
| `key` | `str` | Required. `plugin:<link id>` \| `skill:local:<name>` \| `plugin:orphan:<marketplace>/<plugin>` \| `plugin:orphan:<skill name>` (ref-less orphan) |
| `kind` | `str` | Required. `plugin` \| `skill`. `skill` for a **catalog** link and for a marketplace link whose entry format is `skills`; `plugin` otherwise. Derived from the *format*, never from the transport — nothing routes on `kind` |
| `source` | `str` | Required. `marketplace` \| `bundle` \| `catalog` \| `local` |
| `name` | `str` | Required. The **engine-facing** identity — the plugin directory name or the skill folder name; what `plugin_ref` and the on-disk layout use |
| `display_name` | `str` | Required. For humans |
| `description` | `str` | `""` |
| `version` | `str \| None` | `link.installed_version`; absent on local and orphan rows |
| `marketplace_name` | `str \| None` | `None` on local rows and on a ref-less orphan |
| `plugin_type` | `str \| None` | `claude` \| `codex` \| `skills`. Marketplace rows only, and **clamped through `LLMPluginService._manifest_plugin_type`** — the same helper the manifest builder uses — so a legacy row holding a word from before this vocabulary (`custom`) reads `claude` here exactly as the container treats it. Reading the column raw would let the badge and the container disagree about one install. This is also what `kind` is derived from |
| `link` | `AgentPluginLinkWithUpdateInfo \| None` | Every non-local row. Carries the per-mode toggles, `has_update` and `skill_package_id`, so row mutations stay on the existing plugin routes |
| `skills` | `list[AddonSkillPublic]` | Local row: itself. Catalog row: its one skill. Plugin row: every skill it ships. Empty when the index could not be read |
| `status` | `str` | `ok` \| `warning` \| `error` |
| `status_code` | `str \| None` | The offending skill's issue code, or `orphan` / `source_unavailable` |
| `orphan` | `bool` | `False` |
| `can_share` | `bool` | `False`. Local skills only |
| `can_manage` | `bool` | `False` |
| `published_package_id` | `UUID \| None` | Local skills only |

### `AddonCounts` / `AgentAddonsPublic`

`AddonCounts{plugins, skills, local_skills}` — all `int`, all default `0`, all
counted over **rows** (see the business file for the overlap rules).

`AgentAddonsPublic{agent_id, environment_id, addons, counts, skills_error,
fetched_at, can_add}`. `agent_id` is the only required field; `environment_id`,
`skills_error` and `fetched_at` are all `None` when the agent has no primary
environment.

`SkillEntryPublic`, for reference: `name`, `description`, `source`, `plugin_ref`,
`path`, `has_scripts`, `user_invocable`, `model_invocable`, `size_bytes`, `error`,
`warning`, `secret_paths`, `can_publish`.

---

## `AddonsService` — `backend/app/services/agents/addons_service.py`

Two entry points, both `@staticmethod`:

- `build(session, agent, user, *, environment=None) -> AgentAddonsPublic` — sync.
- `refresh(session, agent, user) -> AgentAddonsPublic` — async; force-refreshes the
  skills index first, then delegates to `build`.

The environment is resolved by `AgentStatusService.get_primary_environment`.

### The fold, step by step

1. `rows: list[AddonPublic]`, `by_ref: dict[str, AddonPublic]`.
2. **Links.** Each link becomes a row (`kind = "skill"` when the source is
   `catalog` **or** the entry's clamped `plugin_type` is `skills`, else
   `"plugin"`); if its ref is non-empty it claims
   `by_ref[ref]`. Ties are **first-wins**, with one exception: an incumbent whose
   own source is unavailable is displaced by a healthy claimant, so a live plugin
   is never hidden behind a dead one.
3. **Local entries.** One row per `source == "local"` index entry.
   `published_package_id` comes from a **single** query over all their names
   (`SkillPackage.source_agent_id == agent.id AND name IN (...)`), skipped
   entirely when the caller cannot build.
4. **Non-local entries.** `ref = entry.plugin_ref or ""`; the owner is
   `by_ref.get(ref)` when the ref is non-empty. With no owner, an orphan row is
   created or reused under `ref or f":{entry.name}"` — the leading colon is what
   keeps ref-less entries in **one row per skill name** instead of all collapsing
   into a single empty-key row. Either way the skill is appended to its owner's
   `skills[]`.
5. `_settle_status` over every row, then sort by
   `(_STATUS_RANK[status], display_name.lower())` with
   `_STATUS_RANK = {error: 0, warning: 1, ok: 2}`.

### `_link_ref` — the folding identity

Returns `f"{marketplace}/{plugin}"`, or `""` when either half is empty.

- `source == marketplace` → `link.marketplace_name or link.snapshot_marketplace_name`,
  `link.plugin_name or link.snapshot_plugin_name`. **Live row preferred,
  snapshot as the fallback.**
- `source == catalog` → `link.snapshot_marketplace_name or CATALOG_MARKETPLACE_NAME`,
  then the plugin snapshot. The fallback to the synthetic `cinna-skills`
  marketplace is not cosmetic: it is exactly what `_build_catalog_entry` writes
  into the manifest, so it is the directory the container reports back. Reading
  the snapshot alone would ref to `""` while the index says
  `cinna-skills/<name>`, and one directory would render as two rows — a real one
  with no skills plus an orphan holding them.
- Any other source → the snapshot columns only.

`get_agent_plugins` fills the live `marketplace_name` / `plugin_name` on every
marketplace link, so a marketplace plugin that ships skills folds into one row.
The snapshot fallback covers the case where the plugin row is gone — a state that
is now reachable and now nameable (see [Cascade and snapshots](#cascade-and-snapshots)).

### `_settle_status` — strict precedence

`row.can_manage = can_build and not row.orphan and row.link is not None` is
computed **first**, before any early return, so it is always set. Then:

1. `orphan` → `error` / `"orphan"`
2. `_source_unavailable` → `error` / `"source_unavailable"`
3. first skill with an `error` → `error` / that code
4. first skill with a `warning` → `warning` / that code
5. otherwise `ok` / `None`

`_source_unavailable(row, unfetchable)`:

| Link source | Unavailable when |
|-------------|------------------|
| `None` (local) | never |
| `catalog` | `skill_package_revision_id IS NULL` or `skill_package_id IS NULL` |
| `marketplace` | `plugin_id IS NULL`, **or** the plugin row exists but the last sync marked it `supported=false` |
| `bundle` | never |

The second marketplace branch is what makes an entry that stopped being
installable read as broken rather than silently doing nothing: the manifest
builder already skips an unsupported entry, so the files never reach the
container.

`_plugin_facts` supplies both the `plugin_type` per link and the `unfetchable`
set in **one** query over the links' non-null `plugin_id`s.

### Capabilities

`can_build = AgentService.can_build(session, user, agent)` is computed once and is
`is_developer(user) and not is_foreign_install(agent) and user_can_access(...)`.
`can_add` on the envelope is exactly that value. `can_share` is `skill.can_publish`
on local rows only.

---

## Routes

### `backend/app/api/routes/agent_skills.py` — router prefix `/agents`, tag `agents`

| Method + path | Deps | Response | Purpose |
|---------------|------|----------|---------|
| `GET /agents/{agent_id}/addons` | `SessionDep`, `CurrentUser` | `AgentAddonsPublic` | Cache-only projection; never wakes a container. No query params |
| `POST /agents/{agent_id}/addons/refresh` | same | `AgentAddonsPublic` | Re-read the index from the environment, then re-project both halves in one call |

Both resolve the agent through the module's `_get_owned_agent`: **404** when the
agent does not exist, **403** unless the caller is a superuser or
`AgentService.user_can_access` passes. A consumer of a foreign install is the
owner of their install row, so they pass and get the read-only projection.

The tag is `agents`, so the generated client names are
`AgentsService.getAgentAddons` and `AgentsService.refreshAgentAddons` — **not**
an `AgentSkillsService`.

The same file's `GET /agents/{id}/skills` and `POST /agents/{id}/skills/refresh`
changed internally only: the entry projection moved into
`AgentSkillsService.entry_to_public`, so the projection and the skills card
cannot drift on what a skill looks like. The response shape is unchanged.

### `backend/app/api/routes/skills.py` — grants, router prefix `/skills`

All three are new, all `SessionDep` + `CurrentUser`, all gated by
`_get_managed_package` (load with a visibility check → 404, then `user_can_manage`
→ **403 `not_publisher`**). **Superusers have no bypass on grants** — they may
delist, never edit somebody else's sharing.

| Method + path | Body | Response | Errors |
|---------------|------|----------|--------|
| `GET /skills/packages/{package_id}/grants` | — | `SkillPackageAccessGrantsPublic` | 403, 404 |
| `POST /skills/packages/{package_id}/grants` | `SkillPackageAccessGrantCreate{email}` | `SkillPackageAccessGrantPublic` | 404 `user_not_found`, 409 `self_grant`; a duplicate is **idempotent 200** |
| `DELETE /skills/packages/{package_id}/grants/{user_id}` | — | 204 | 404 `grant_not_found` |

Revoke is keyed on the **user id**, not the grant id.

`POST /agents/{agent_id}/skills/{name}/publish` gained one field on its body
(`grant_emails`) and one refusal that field can trigger — **409
`grants_require_users_visibility`** when the publish would leave the package on a
visibility other than `users` (see [Visibility and
grants](#visibility-and-grants)). Otherwise unchanged.

`POST /skills/packages/{package_id}/delist` was **hardened on touch**: it now
checks `is_superuser` *before* loading the package, then loads without a
visibility check. Previously it loaded through the visibility-gated path, which
was both an existence oracle and the reason a `users` package could not be
delisted.

---

## Models

### `skill_package_access_grant` (new) — `backend/app/models/skills/skill_package_access_grant.py`

A field-for-field mirror of `bundle_access_grant`.

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | UUID | PK |
| `package_id` | UUID | FK `skill_package.id`, `ON DELETE CASCADE`, not null |
| `user_id` | UUID | FK `user.id`, `ON DELETE CASCADE`, not null |
| `granted_by_user_id` | UUID | FK `user.id`, `ON DELETE SET NULL`, nullable |
| `created_at` | `DateTime` | |

`uq_skill_grant_package_user (package_id, user_id)`, `ix_skill_grant_package`,
`ix_skill_grant_user`.

Schemas in `backend/app/models/skills/schemas.py`:
`SkillPackageAccessGrantPublic`, `SkillPackageAccessGrantCreate{email}`,
`SkillPackageAccessGrantsPublic{data, count}`. `SkillPackageEntry` gained
`is_granted`; `SkillPublishRequest` gained `grant_emails: list[str]` (max 50).

### `skill_package.visibility` (extended, no DDL)

`SkillPackageVisibility` gained `USERS = "users"`. The column is already
`String(16)` and the level set is enforced in application code
(`_normalise_visibility`, 422 `invalid_visibility` on anything else).

### `llm_plugin_marketplace_plugin` (two new columns)

| Column | Type | Notes |
|--------|------|-------|
| `supported` | `bool` | not null, default `True`. Re-derived on **every** sync, never sticky |
| `unsupported_reason` | `str(64)` | nullable. One of five codes |

`LLMPluginMarketplacePluginPublic` mirrors both and adds
`skill_summary: PluginSkillSummary | None`.

### `llm_plugin_marketplace.type`

`MarketplaceType = Literal["claude", "codex", "skills"]` on create and update, so
an unknown format is a **422 at the API boundary**. The stored column stays a
plain string, and `plugin_type` on each entry is written from it on every upsert.

---

## Cascade and snapshots

Two changes in `backend/app/models/plugins/llm_plugin.py` and the install path
that, together, make an orphaned link nameable.

**The cascade fix.** `LLMPluginMarketplacePlugin.agent_links` was
`sa_relationship_kwargs={"cascade": "all, delete-orphan"}`. The ORM cascade ran in
Python and deleted the links *before* the database's own rule could apply, so the
`plugin_id` column's `ondelete="SET NULL"` never fired — deleting a marketplace
silently uninstalled every user's plugins (memory:
`project_plugin_link_cascade_bug`). It is now
`sa_relationship_kwargs={"passive_deletes": True}`, which defers to the FK. For
contrast, `LLMPluginMarketplace.plugins` still cascades — deleting a marketplace
*should* delete its entry rows, and that is what NULLs the links.

**The snapshots.** `install_plugin_for_agent` now writes
`snapshot_marketplace_name` and `snapshot_plugin_name` on the marketplace path
too (the bundle and catalog paths always did). Data-only migration
`c8d2e5b71a04_backfill_marketplace_link_name_snapshots` backfills every existing
`source='marketplace'` link whose `plugin_id` still resolves and whose snapshots
are NULL. A link whose plugin row is *already* gone cannot be reconstructed and is
left alone; the downgrade is a deliberate no-op, since erasing true values would
only re-create the defect.

**The third snapshot — `snapshot_repository_url`.** A nullable column on
`AgentPluginLink`, written by the marketplace install path only
(`snapshot_repository_url=marketplace.url`) and backfilled by migration
`d7b41e0c9a35` from the live marketplace rows — same join shape and same
`source='marketplace'` restriction as `c8d2e5b71a04`. Bundle and catalog links get
none; they have no upstream repository of their own.

It exists because the two *name* snapshots are not proof of provenance. The
`created_at` guard rejects only a marketplace row **younger** than the link, and a
rename goes the other way: `idx_marketplace_name_unique` frees a name precisely
when its holder is deleted — the event that orphans the links — and
`sync_marketplace` reassigns `marketplace.name` from the repository's own
`marketplace.json` on every sync. So an **older** marketplace row could acquire a
deleted one's freed name, adopt its orphans, and from the next environment sync
deliver code from a repository the agent's owner never chose. The name backfill
widened that from "links created since the install path started writing names" to
every marketplace install in the database, which is why the URL column shipped
alongside it and not later. Dropping the column re-opens the hole; the downgrade
is a schema rollback, never a cleanup.

**Two keepers of the truth**, both wrapped so neither can fail a sync:

- `_rename_link_snapshots(session, marketplace, previous_name)` — follows a
  marketplace rename through to its **live** links.
- `_reattach_orphaned_links(session, marketplace)` — re-points orphans when their
  entry reappears. Candidates are `plugin_id IS NULL` + `source == marketplace` +
  both snapshot names, and adoption now requires **all three** of:

  1. the directory identity — the two name snapshots, which is what makes the row
     nameable and what the environment has on disk;
  2. **repository identity** — `_same_repository(link.snapshot_repository_url,
     marketplace.url)`, the identity a rename cannot transfer. Asserted twice:
     `snapshot_repository_url IS NOT NULL` in the SQL so the candidate set never
     contains a URL-less link, and the normalising comparison in Python (no `=` in
     Postgres would normalise). **A NULL fails closed** — those are marketplace
     installs made before the column existed *and* already orphaned before
     `d7b41e0c9a35` ran, so there was no live row to backfill from. They are never
     adopted; the remedy is uninstall and re-install;
  3. `link.created_at >= marketplace.created_at` — **kept, not replaced.** Dropping
     a cheap independent condition because a stronger one arrived is how one
     mistake in the stronger one becomes total.

  `_same_repository` / `_canonical_repository_url` normalise only what this module
  already treats as insignificant: SSH→HTTPS for the three public hosts
  (`_normalize_public_git_url`) and a trailing `/` or `.git`. Host casing, `http`
  vs `https`, a `user@` prefix inside an HTTPS URL, and SSH on any host outside the
  public three are **not** normalised and therefore mean "no match" — a manual
  re-install, which is the safe direction to fail.

  **The three conditions are independent, so a matching repository is necessary
  and not sufficient.** Adopted: an entry that vanished upstream and returned on a
  marketplace row that was never deleted, and an older row renamed onto a freed
  name whose repository matches the install's. Refused: a different repository
  under a reused name, a NULL URL, and — by the age guard, before the URL is ever
  compared — a marketplace **deleted and re-registered from the same repository**,
  because the new row postdates the orphaned link. Refusing is the safe direction;
  the remedy is uninstall and re-install. *(`_reattach_orphaned_links`' own
  docstring currently overclaims here, saying the delete-and-re-add case is
  adopted. It is not — `agents_addons_projection_test.py` Scenario 9 pins the
  behaviour, including a differential pair identical but for the URL, and a
  correction to the docstring is in flight.)*

  A `taken` set of existing `(agent_id, plugin_id)` pairs keeps
  `idx_agent_plugin_unique` intact, and re-attaching to an entry the last sync
  marked `supported=false` is allowed and inert.

---

## Marketplace parsers — `backend/app/services/plugins/llm_plugin_service.py`

`_get_parser_for_type` is a dict lookup over `claude` / `codex` / `skills` that
**raises** `MarketplaceFormatError` on a miss — it no longer falls back to the
Claude parser. `MarketplaceCatalogError` subclasses it with code
`marketplace_catalog_unreadable`; `routes/llm_plugins.py` maps both to **422**
with `{code, message}`.

Because the parser raises **before** `_upsert_plugins` runs, and stale-row
deletion lives only inside `_upsert_plugins`, an unreadable catalog **deletes
nothing**. The marketplace is left `status=error` with the sentence.

### `_parse_codex_marketplace`

Reads `.agents/plugins/marketplace.json`; metadata from `name` /
`interface.displayName`, `description` / `interface.description`, `author.*`.
Each entry stores its raw dict verbatim in `config` and branches on
`source.source`:

| `source.source` | Result |
|-----------------|--------|
| `local` | safe relative `source_path`; then `_apply_codex_plugin_manifest` |
| `url` / `git-subdir` | `source_type=url`, `source_url`, `source_branch = ref or "main"`, `source_commit_hash = sha`; `git-subdir` also sets a safe `source_path` |
| `npm` | `supported=false`, `npm_source` |
| anything else | `supported=false`, `unknown_source` |

`_apply_codex_plugin_manifest` reads `<dir>/.codex-plugin/plugin.json` for
`description`, `version`, `author_*`, `homepage`, and copies `interface` and
`policy` into `config`. A manifest that cannot be read is logged and treated as
empty — **not** as unsupported. A manifest declaring `apps` and none of
`skills` / `mcpServers` / `hooks` / `commands` / `agents` is
`app_connector_only`. `config["skills"]` is always filled from the plugin's own
`skills/<name>/SKILL.md` folders (symlink-refusing).

### `_parse_skills_marketplace`

No catalog file. Walks `skills/<name>/SKILL.md`, or `<name>/SKILL.md` at the repo
root when there is no `skills/` directory. Each folder is parsed by the **backend
copy** of the vendored `skill_manifest` parser — the same restricted-subset
frontmatter reader env-core uses, reused rather than copied a third time — and
emitted as one plugin entry with `category="skill"`, `plugin_type="skills"`,
`source_type=local`, and `config={"skill": {name, description, has_scripts}}`.
An invalid `SKILL.md` becomes `supported=false, no_skill_md` so the admin can see
the repo is half-broken and a later upstream fix re-syncs into a supported row.

**It returns no marketplace name, deliberately.** A repository like this carries
nothing authoritative to name itself with (only a README heading), and
`marketplace.name` is not a label — it is the on-disk directory segment every
install path writes into `plugins/<marketplace>/`. Letting an upstream README
edit rename it would move every install's directory on the next sync. If a
display name is ever wanted it belongs at **create** time, never at sync time.

Zero valid skill folders raises `MarketplaceCatalogError`.

### The five `unsupported_reason` codes

Set only through `_mark_unsupported` (plus the two direct writes for
`app_connector_only` and the skills parser's verdict), and persisted on both the
insert and update branches of `_upsert_plugins`.

| Code | Meaning |
|------|---------|
| `npm_source` | published to npm, which agent environments cannot install from |
| `app_connector_only` | declares app connectors only, which this platform does not run |
| `no_skill_md` | no valid `SKILL.md`, so there is nothing to install |
| `unknown_source` | declares a source kind this platform cannot fetch from |
| `unsafe_path` | points outside its repository (absolute or `..`), so it cannot be fetched safely |

One sentence each in `UNSUPPORTED_REASON_SENTENCES`, mirrored client-side in
`frontend/src/utils/marketplace.ts` so the create dialog, the admin table, the
entries table and the Add-addon dialog cannot print four different words for one
row. Nothing on the client matches on prose — the server owns the code, and
answers a refused install with its own sentence.

### Refusals on the install path

- `install_plugin_for_agent` → **409** `{code: "plugin_unsupported", message,
  reason}` when `plugin.supported` is false, checked **before** the
  already-installed guard.
- `build_plugin_manifest` also skips an unsupported entry for an existing install,
  which is what makes its addon row read `source_unavailable`.
- `upgrade_agent_plugin` on a link whose `plugin` is gone → **409**
  `{code: "source_unavailable"}` telling the user to uninstall. A `catalog` link
  delegates to `SkillCatalogService.upgrade_link` and lets `SkillCatalogError`
  propagate through the shared `http_error_for` map.

---

## Publish path — `backend/app/services/skills/skill_catalog_service.py`

### `_resolve_skill_dir` — the cloud workspace, and nothing else

1. `SKILL_NAME_RE` guard on the name → 404 `skill_not_found` (this is what makes
   the path join safe).
2. `session.get(AgentEnvironment, agent.active_environment_id)` → 409
   `no_environment` when there is none.
3. `Path(settings.ENV_INSTANCES_DIR) / str(env.id) / WORKSPACE_ROOT_REL` → 409
   `workspace_unavailable` when it is not a directory.
4. `workspace_root / "skills" / skill_name` → 404 `skill_not_found` when it is
   missing **or is a symlink**.

There is no local-folder or upload branch anywhere in the publish path. The
workspace is read host-side off the bind mount, which is why a **suspended**
environment publishes exactly like a running one and no container call is made.

### The refusals, and the constant that is not one of them

| Check | Code | Status |
|-------|------|--------|
| `entry.error is not None` | `skill_invalid` | 422 |
| `entry.secret_paths` non-empty | `skill_contains_secrets` (+ `paths`) | 422 |
| `entry.size_bytes > MAX_PACKAGE_BYTES` | `skill_too_large` | 422 |
| resolved `grant_targets` while the **effective** visibility is not `users` | `grants_require_users_visibility` | 409 |

The first three are content checks and run before the publish lock; the fourth is
described under [Visibility and grants](#visibility-and-grants) and runs *inside*
it. All four run before any write.

`MAX_PACKAGE_BYTES = DEFAULT_MAX_TOTAL_BYTES = 16 * 1024 * 1024`. The comparison
is **strictly `>`**, so a tree of exactly 16,777,216 bytes publishes.

`MAX_BODY_BYTES = 64 * 1024` is a **different** constant: a cap on the `SKILL.md`
body text that produces a non-blocking `oversized` **warning**, ranked last in
`WARNING_PRECEDENCE`. The publish path never reads `entry.warning` at all.

`SkillEntry.is_publishable` is `error is None and not secret_paths` — it ignores
the size cap, so it and the server's three-way gate agree only on the first two.

### `is_secret_filename` reads names, never contents

Three name-shaped sources, all applied to a **basename**: exact matches
(`credentials.json`, `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519`), globs
(`*.pem`, `*.p12`, `*.pfx`, `*.key`) and the dotenv rule, whose clause tests are
only `basename_equals` / `basename_prefix` / `basename_suffix`. An unknown clause
counts as a hit — the failure direction is fail-safe.

The only file the module opens anywhere is `SKILL.md`, and only to parse its
frontmatter. Sizes come from `stat()`.

### Revisions

`skill_package_revision` rows are never updated. A publish takes a per-`(publisher,
skill name)` `asyncio.Lock`, finds-or-creates the package, allocates
`max(revision_number) + 1` under that lock, writes the snapshot off the event loop
(`safe_copytree`, symlink-refusing at every depth, into `<rev>.tmp/` then
`shutil.move` into `<rev>/`, so no reader sees a half-written revision), inserts
the revision row, and then repoints `package.latest_revision_id`.

**Known window:** the revision commits before the package commit, so a failure in
the final commit leaves an orphan revision. Documented in code, unclosed.

### Visibility and grants

`_normalise_visibility` returns `None` for `None`, else validates against the
three levels (422 `invalid_visibility`). `_apply_publish_to_package` assigns
`package.visibility` **only when the incoming value is non-null**, which is why a
bare publish leaves a first-time package at its column default, `private`.

`user_can_see`: publisher → true; `PUBLIC` → `is_listed or is_superuser`;
**`USERS` → `is_listed AND a grant exists`** (no superuser bypass — delisting
beats a grant); anything else → false.

`list_catalog` unions three statements: public + listed, **granted + listed +
`visibility == users`**, and the caller's own packages regardless of listing.
Rows with no `latest_revision_id` are dropped. Grant membership for the whole page
is fetched in **one** grouped query and surfaced as `is_granted`.

`resolve_grant_user` matches `func.lower(User.email) == func.lower(email)`
(memory: `project_email_case_sensitivity_bug`); `grant_to_user` returns the
existing row on a duplicate.

**The `grant_emails` visibility guard** (`skill_catalog_service.py:296-346`).
`_resolve_grant_targets` still runs before the lock — resolving every address,
raising `user_not_found` before any write, and silently dropping the publisher's
own address. What is new sits *inside* the publish lock and *before*
`_resolve_package`, which is the first statement that commits anything:

```
if grant_targets:
    existing_package = _find_publisher_package(...)          # read-only
    effective = visibility or (existing_package.visibility
                               if existing_package else PRIVATE)
    if effective != USERS:
        raise SkillCatalogError("grants_require_users_visibility", ...)   # 409
```

Three deliberate choices:

- **Effective, not requested.** `visibility=None` means "leave the package's
  current visibility alone", so re-publishing an already-`USERS` package with more
  addresses must keep working; adding addresses to a `PUBLIC` package without
  restating the visibility must not. Hence the read-only package lookup, and
  `PRIVATE` — the new-package default — when there is none.
- **Resolved targets, not the raw list.** Those are the rows this publish would
  actually write; a request naming only the publisher resolves to `[]`, writes
  nothing, and is not refused.
- **Inside the lock.** The lock keys on the same `(publisher, skill name)` this
  reads the package for, so a concurrent publish cannot change the visibility
  between the read and the grant write. A concurrent `update_package` still can —
  that path takes no lock — which is a narrower race than the one this closes and
  inherent to any check-then-act.

`grant_access` (the standalone route) is deliberately **permissive** about
visibility: granting on a still-`private` package and then flipping it to `users`
is ordinary preparation, and there the whole request is "let this person see it".
Publish refuses the *side effect*; the grant route answers the request.

> **Residual gap.** Grant rows written by publishes made **before** this guard are
> still in the table. Nothing cleans them up, so a database that ran an earlier
> build can hold inert rows that a later flip to `users` would activate. Revoke is
> the only remedy.

Note the shape difference between the two entry points: the dedicated
`POST .../grants` route passes a `publisher_user_id`, so granting to yourself is
**409 `self_grant`**; publish passes `None` and silently drops the publisher's own
address instead, because a publish naming its own author is a typo, not a policy
violation.

---

## Container-side normalisation — `agent_env_service.py` (env-core)

Both engines locate a plugin through `.claude-plugin/plugin.json`. A Codex repo
names its manifest elsewhere and a `skills`-format repo has none at all.
`_ensure_plugin_manifest(plugin_dir, entry, ref)` normalises the four cases into
that one file, in strict precedence:

1. `.claude-plugin/plugin.json` exists **and parses as a JSON object** → return.
   An authored manifest is never read-modified, never overwritten, never followed
   through a symlink. A `name` disagreeing with the directory is logged (Claude
   Code requires them to match) but left as the author wrote it. A file that is
   not a JSON object counts as **absent** — no engine can load it, and the
   likeliest way one appears is a truncated write of our own.
2. `.codex-plugin/plugin.json` exists → synthesise from it.
3. `SKILL.md` at the directory root → a bare-skill plugin; synthesise with
   `name=<dir>`. Nothing is moved: the `skills`-format install path already
   copies such a tree into `skills/<name>/`, and this branch catches the same
   repo arriving through an older manifest entry with no `plugin_type`.
4. Otherwise → a minimal manifest from the entry, so the SDK accepts the
   directory instead of ignoring it silently.

Branches 3 and 4 read a `description` off the manifest entry when the fetched tree
carries none of its own — `build_plugin_manifest` now emits the marketplace row's
`description` for exactly this, so a bare-skill or `skills`-format plugin gets the
sentence its catalog declared instead of the placeholder `Skill '<name>'`. **Not
retroactive:** rule 1 never overwrites a manifest already on disk, so a plugin
normalised before the key existed keeps its placeholder until it is re-fetched.
Nothing else consumes the key, and the addon row's own `description` still comes
from the database row.

### The catalog exception

A **catalog** install does not run that chain. It calls
`_write_catalog_plugin_json`, which writes the manifest **unconditionally on every
sync**, because a package's name and description are metadata a publisher can edit
without cutting a new revision — rule 1 would pin the first install's text forever
and a rename would never be delivered.

Both paths land through the same writer, `_write_plugin_manifest`: `mkstemp` in
the same directory (`O_CREAT|O_EXCL`) then `os.replace`, so a disk-full or
read-only remount leaves either the previous file intact or the new one whole,
never a truncated manifest — and neither step can be redirected through a
symlink. `bundle` sources get neither treatment.

### `manifest_write_failed` does not always deactivate

A failed write returns `manifest_write_failed`, and the plugin is reported as a
failed install. Whether it is *also* dropped from `settings.json` is decided by
`_plugin_manifest_is_loadable`: a failed write into a **staging** tree leaves the
previously installed, perfectly loadable revision in place, and deactivating that
would punish a working plugin for a transient disk error. Only directories that
fail the check join the `blocked` set the settings writer skips.

---

## Migrations

| Revision | Down-revision | Contents |
|----------|---------------|----------|
| `a3f1c07b9d42_add_skill_package_grants_and_plugin_support_flags` | `b71f4a9c2d30` | Creates `skill_package_access_grant` with its unique constraint and two indexes; adds `llm_plugin_marketplace_plugin.supported` (`NOT NULL`, `server_default true`, dropped after backfill) and `.unsupported_reason` (`VARCHAR(64)` nullable). No DDL for `skill_package.visibility` — the column is already `VARCHAR(16)` and the level set is enforced in code |
| `c8d2e5b71a04_backfill_marketplace_link_name_snapshots` | `a3f1c07b9d42` | **Data only.** Fills `snapshot_marketplace_name` / `snapshot_plugin_name` on `source='marketplace'` links whose `plugin_id` still resolves. Downgrade is a deliberate no-op |
| `d7b41e0c9a35_add_agent_plugin_link_repository_snapshot` | `c8d2e5b71a04` | Adds `agent_plugin_link.snapshot_repository_url` (nullable `VARCHAR`) and backfills it from `llm_plugin_marketplace.url` for `source='marketplace'` links whose `plugin_id` still resolves. A link already orphaned when this runs gets no URL and can never be re-adopted — deliberate. The downgrade drops the column, which re-opens the rename hole it closes |

---

## Frontend — `frontend/src/components/Agents/Addons/`

| File | Role |
|------|------|
| `AgentAddonsTab.tsx` | The tab body — a 1-column `space-y-6` stack, not a card grid. Owns the live `PLUGIN_SYNC_WARNING` banner (rebuilt as an `Alert` on `--warning` tokens) and the sync-issues dialog |
| `AddonsCard.tsx` | The "Plugins and skills" card: `PreviewList` capped at 5 + "Show all (N)", header actions, the counts line, the `skills_error` alert |
| `AddonRow.tsx` | The single row shape, grown from `InstalledPluginRow` and absorbing `SkillRow`. **No `meta` line on any row** — in a mixed list a meta line on half the rows makes one list look like two, which is the seam this projection removes; every description goes to `RowInfo` |
| `useAddonRowMutations.ts` | Toggle / upgrade / uninstall, owned by the row |
| `AllAddonsSheet.tsx` | The full list |
| `AddonDetailDialog.tsx` | Facts + the row's skills + the `SKILL.md` pane (shared `Agents/SkillContentBody.tsx`) |
| `AddAddonDialog.tsx`, `AddAddonChooseStep.tsx`, `AddAddonModesStep.tsx`, `AddAddonResultRow.tsx` | The two-step add wizard over plugin discovery + the skills catalog |
| `ShareSkillDialog.tsx`, `ShareSkillSuccessPanel.tsx` | Publish / re-publish with the visibility segment and the people picker |
| `frontend/src/utils/addons.ts` | `addonsQueryKey`, `invalidateAddons`, `addonRowStatus`, `addonSourceFlag`, `addonFormatLabel`, `addonNoun`, `buildAddonResults`, `isAddonResultBlocked` — shared so no call site re-derives what a dot means |
| `frontend/src/utils/marketplace.ts` | `MarketplaceFormat` (derived from the generated client, never re-declared), the format labels, `unsupportedReasonSentence` |
| `frontend/src/hooks/useMarketplaceSync.ts` | The one sync mutation for all three admin call sites, with one invalidation set |

Deleted at parity: `AgentPluginsTab.tsx`, `InstalledPluginsCard.tsx`,
`InstalledPluginRow.tsx`, `AllInstalledPluginsSheet.tsx`, `InstallPluginModal.tsx`,
`PluginCard.tsx`, `AgentSkillsCard.tsx`, `AllSkillsSheet.tsx`, `SkillRow.tsx`,
`SkillContentDialog.tsx`, `PublishSkillDialog.tsx`.

New catalog surfaces: `Catalog/SkillPackageAccessCard.tsx`,
`SkillPackageGrantRow.tsx`, `AddSkillPackageGrantDialog.tsx`,
`AllSkillPackageGrantsSheet.tsx`.

**Tab registration.** `routes/_layout/agent/$agentId.tsx` registers
`{ value: "addons", title: "Addons" }` and — load-bearing — adds `"addons"` to the
`agentUserTabs` set, so a consumer of a foreign install keeps the read-only view
that the removed Skills card used to give them.

### Query keys

| Key | Reader |
|-----|--------|
| `["agent", agentId, "addons"]` | the projection |
| `["agent", agentId, "skills"]`, `["agent", agentId, "plugins"]` | the two halves, still read on their own by the command popup and `AddSkillToAgentDialog` |
| `["discover-plugins", query, limit]`, `["skills-catalog"]` | the add wizard's two searches |
| `["skills-catalog", "package", packageId]` | package facts in the detail and share dialogs |

`invalidateAddons(queryClient, agentId, { catalog })` invalidates all three agent
keys in one call — the projection is a **join**, so a mutation on either half
invalidates it, and the halves still have their own consumers. `catalog: true`
additionally refreshes the skills catalog's `install_count` /
`installed_in_agent_ids`.

---

## Testing

| File | Covers |
|------|--------|
| `backend/tests/api/agents/core/agents_addons_projection_test.py` | The fold: one row per catalog install, plugin skills folding into their plugin row, orphan rows (both kinds), `skills_error` while plugin rows still return, capabilities false on a foreign install and for non-developers |
| `backend/tests/api/agents/core/agents_marketplace_formats_test.py` | Codex and skills parsers, `supported` / `unsupported_reason`, unknown type 422, path traversal refused at sync |
| `backend/tests/api/agents/core/agents_skills_catalog_test.py` | Grants, `users` visibility, publish with `grant_emails` |
| `backend/tests/api/agents/core/agents_skills_catalog_install_test.py` | Install / upgrade / uninstall through the catalog link |
| `backend/tests/api/agents/core/agents_resilient_plugins_test.py` | Install refusal for unsupported entries; the cascade and snapshot behaviour |
| `backend/tests/unit/test_plugin_manifest_normaliser.py` | The normaliser's four branches, the catalog exception, `manifest_write_failed` |
| `backend/tests/utils/addons.py`, `backend/tests/utils/llm_plugin.py` | Fixtures |

Run: `docker compose exec backend python -m pytest tests/api/agents/core/ -v`.
