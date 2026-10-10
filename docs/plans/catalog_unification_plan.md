# One catalog for agents and skills: core phases C1–C5

Inputs: `drafts/catalog_unification/README.md` (U1–U13, §5 contract, §8 decisions) and `core.md`.
Code refs verified at `d5bb0a16`. C0 (kit contract 1.10.0, `docs/local_agent_kit/`) is done by another
agent and is not in this plan. Out of scope: cloud twins, mirrors, Suggest, editing lease.

## 0. Index

### 0.1 Working-tree rules
- Order C1 → C2 → C3 → C4 → C5. Each phase is one commit (backend, client regen, frontend, tests, docs) and is green on its own:
  `alembic upgrade head` + `downgrade -1` + `upgrade head` clean, phase tests, and the regression groups listed per phase.
- After each phase, update `drafts/catalog_unification/core-status.md`: commit, routes and fields shipped, deviations (§0.3).
  Copy any §5 deviation into §5 of `drafts/catalog_unification/README.md` too.
- Do not touch `docs/local_agent_kit/` or `backend/app/workspace/knowledge/local-kit/`. They belong to C0.
- New backend package: `backend/app/services/catalog/`. New models package: `backend/app/models/catalog/`, re-exported in `models/__init__.py`.
  The git transport stays in `backend/app/services/library/` (`git_http_service.py`, `git_repo_storage.py`, `push_policy.py`, `exceptions.py`)
  because the hook imports `app.services.library.push_policy`. `library_service.py`, `library_access_service.py` and `library_events.py` are deleted in C2.

### 0.2 Decisions taken (beyond U1–U13; recommendation applied)
| # | Decision |
|---|---|
| P1 | **Effective access** (one function for all kinds): inactive user ⇒ none. `g` = max over grants matching the user (a user grant, or a role grant on `user.role`; the `admin` role also matches superusers). `edit`/`maintain` count at any visibility and listing state. A `use` grant counts only when `visibility='users'` and `is_listed`. `public` and `is_listed` gives everyone `use`. `private` gives nothing beyond edit/maintain grants (stored `use` rows stay but do nothing, as today). A superuser may see a delisted public item (the skill rule, extended to bundles) |
| P2 | **Management** (`PATCH`, grants, delete through `/catalog/items`) = `maintain` **or superuser** (the library rule). This replaces the admin Team Library page. The old `/bundles/*` and `/skills/packages/*` routes keep their gates, with "publisher" read as "holds `maintain`" |
| P3 | Listing for a superuser follows grants. `GET /catalog/items?scope=all` (superuser only, **additive to §5**) returns every item. Superusers get `access='maintain'` there |
| P4 | Items with no revision (a desktop agent before its first push) are listed only to people with `edit` or `maintain` |
| P5 | The old `GET /catalog/` and `GET /bundles/` lists show only `runs_on='cloud'`. Every cloud route on a desktop bundle answers 409 `{"code":"desktop_only"}` (C2 list) |
| P6 | **Phase split vs README §6:** C2 ships the whole of §5.1 except local installs, because `/library` disappears in C2 and the desktop needs a listing that same day. C3 adds `catalog_local_install`, local counts and guards, and `catalog.updated` for cloud publishes, and does the web UI |
| P7 | The C2 migration **converts** each existing `git_repo(kind='agent')` into an `AgentBundle(runs_on='desktop')`. Bare repos stay where they are and keep their `repo_id`/clone URL. The steps are in §2.1. Downgrade is lossy |
| P8 | A desktop revision's `content_hash` = the git **tree** id of the pushed commit, `snapshot_path` = NULL, `version` = NULL ("Version N" is `revision_number`) |
| P9 | `catalog.updated`/`catalog.granted` go to users reached **through grants** (role grants expanded) plus, from C3, local and cloud installers. For public items there is no fan-out to everyone; clients poll |
| P10 | `device_id` in local-install bodies is opaque client data (≤128 chars) and is stored as sent. It is not bound to the token's `external_client_id` |
| P11 | The skill upload archive is a tar.gz with one top-level directory equal to the skill name (`tar -C skills -czf x.tgz <name>`) |
| P12 | `DELETE /catalog/items/{uuid}` on a skill package answers 409 while any agent of **another** person links it, or another person has a local install. The caller's own links also give 409 ("Remove it from your agents first"); links are never deleted silently |

### 0.3 Places where README §5 cannot be implemented as written (minimal deviation)
1. `GET /catalog/items/{uuid}`: bundles and packages live in two tables. Core looks the uuid up in `agent_bundle` and then in `skill_package`. No change on the wire.
2. `CatalogItem.latest.published_by` has no shape in §5. Shipped as `{user_id, display_name} | null`.
3. `CatalogItem.name` for a cloud bundle has no source column. Shipped as `display_name`. For a desktop agent it is `slug`; for a skill, the folder name.
4. `POST /skills/packages/publish`: `meta` is a multipart **form field holding JSON**. A later publish of a package the caller already publishes, sent without `package_uuid`, gets 409 `package_exists` with `package_uuid` in the body.
5. Additive only: `?scope=all` (P3) and `CatalogGrant.display_name`.

### 0.4 Phase table
| Phase | BE/FE | Main files | Migration | Tests (new files) | Done when |
|---|---|---|---|---|---|
| C1 one access model | BE + regen (no UI) | `models/catalog/catalog_access.py`, `services/catalog/catalog_access_service.py`, `routes/catalog_items.py` (grants only), `catalog_service.py`, `skill_catalog_service.py`, `bundle_service.py`, `publish_service.py`, `routes/bundles.py`, `routes/skills.py`, `models/events/event.py` | `catalog_access_grants`: principal+level on both grant tables + publisher→maintain | `tests/migrations/catalog_access_grants_test.py`, `tests/api/catalog/catalog_access_test.py`, `tests/api/catalog/catalog_grants_test.py` | old grants keep their meaning; role grants work; publish needs `edit`; old grant routes are green |
| C2 desktop agents are bundles | BE + regen + **FE removal** | `services/catalog/{desktop_agent_service,desktop_agent_revisions,catalog_item_service,catalog_events}.py`, `routes/catalog_items.py`, `routes/git_http.py`, `git_http_service.py`, `push_policy.py`, `install_service.py`, `catalog.py`, `bundles.py`, `git_source_service.py`, removal of library | `desktop_bundles`: bundle `runs_on/git_repo_id/manifest_id/slug`, revision `git_commit_sha` + nullable `snapshot_path`, slim `git_repo`, drop `git_repo_grant` | `tests/api/catalog/{catalog_desktop_agents_test,catalog_desktop_git_test,catalog_desktop_only_guards_test,catalog_items_test}.py`, `tests/unit/test_push_policy_bound_manifest_id.py`, `tests/unit/test_desktop_bundle_id.py`; migration = manual alembic check (§2.8) | create → push with real git → Version 1 in `/catalog/items`; id change refused; cloud install refused; `/library` gone |
| C3 unified listing, local installs, web | BE + regen + **FE** | `models/catalog/catalog_local_install.py`, `services/catalog/local_install_service.py`, `catalog_item_service.py`, `publish_service.py`, `routes/catalog_items.py`, `frontend/src/routes/_layout/catalog*`, `components/Catalog/*`, `AdminMenu.tsx` | `catalog_local_install` table | `tests/api/catalog/{catalog_local_installs_test,catalog_listing_matrix_test,catalog_events_test}.py` | web `/catalog` lists every kind; maintainers manage People, visibility and delete; delete guard counts local installs |
| C4 skills from a desktop | BE + regen (no UI) | `skill_catalog_service.py` (split publish core), `services/skills/skill_upload.py`, `routes/skills.py` | none | `tests/api/catalog/{catalog_skill_upload_publish_test,catalog_skill_download_via_agent_test}.py` | upload publish (valid, invalid, secret, oversize, unsafe, edit-only republish); U7 download |
| C5 provision-local | BE + regen (no UI) | `credential_provisioner.py`, `desktop_credential_service.py`, `skill_catalog_service.py`, `routes/skills.py` | none | `tests/api/catalog/catalog_skill_provision_local_test.py` | a publisher-provided slot reaches `/external/credentials`; desktop publishes may resolve `publisher` |

Frontend surfaces needing a UI Specification (designer): **C3 only** (web catalog list with kind filter, desktop-agent detail, maintainer People/visibility/delete, admin catalog management). C2 deletes the admin Team Library page and builds nothing.

`tests/api/catalog/` is a new domain directory. Its `README.md` (C1) explains: API-only tests; the git-bridge seam copied from `tests/api/library/README.md`; scenario files per phase.

---

## 1. C1: one access model

### 1.1 Models: `backend/app/models/catalog/catalog_access.py` (new)
- `CatalogAccess`: `USE='use' < EDIT='edit' < MAINTAIN='maintain'`, plus `rank()` and `max()` (port of `LibraryAccess`, `models/library/git_repo.py:52`).
- `CatalogPrincipalKind`: `USER`, `ROLE`. There is no `everyone`; that is `visibility='public'`.
- Schemas: `CatalogPrincipalUser{user_id}` and `CatalogPrincipalRole{role}` (both `extra=forbid`); `CatalogGrantIn{principal, access}`;
  `CatalogGrant{principal, access, display_name, granted_by: {user_id, display_name}|None, created_at}`; `CatalogMaintainer{user_id, display_name}`.
- `BundleAccessGrant` (`models/bundles/bundle_access_grant.py:15`) and `SkillPackageAccessGrant` (`models/skills/skill_package_access_grant.py:21`) gain:
  - `principal_kind: str(16)`, NOT NULL, default `'user'`
  - `principal_id: str(64)`, NOT NULL (user uuid as text, or the role value)
  - `access: str(16)`, NOT NULL, default `'use'`
  - `user_id` becomes **nullable** (still FK CASCADE, set only for user grants)
  - constraints: drop `uq_bundle_grant_bundle_user` / `uq_skill_grant_package_user`; add `uq_bundle_grant_principal(bundle_id, principal_kind, principal_id)` / `uq_skill_grant_principal(package_id, principal_kind, principal_id)`; add check `ck_*_grant_user_fk`: `(principal_kind='user') = (user_id IS NOT NULL)`.
- `BundleAccessGrantPublic` and `SkillPackageAccessGrantPublic` gain `access: str = 'use'`. Old list routes return **user** grants only.

### 1.2 Migration `catalog_access_grants` (one revision, both tables)
Upgrade, for each table:
1. Add the columns with server defaults; `UPDATE … SET principal_id = user_id::text`.
2. Make `user_id` nullable; swap the unique constraints; add the check.
3. Publisher becomes maintainer. Bundles: `UPDATE grant SET access='maintain'` where `user_id = agent_bundle.publisher_user_id`, then `INSERT` a `user/maintain` row for each bundle whose `publisher_user_id IS NOT NULL` and has no row yet (`granted_by_user_id` = publisher). Skills: the same from `skill_package.publisher_user_id`.

Downgrade:
1. Delete role rows.
2. Delete rows where the user is the item's publisher **and** the row was created by the upgrade (`access='maintain' AND granted_by_user_id = user_id`).
3. Any other `edit`/`maintain` user row falls back to a plain grant (lossy; documented in the migration docstring).
4. Restore the old unique constraint, `user_id` NOT NULL, and drop the columns.

### 1.3 Service `backend/app/services/catalog/catalog_access_service.py` (new)
Port `library_access_service.py:49–283`, made generic over a kind adapter:
```
CatalogKindSpec(kind: 'bundle'|'skill', item_model, grant_model, grant_fk: str)  # BUNDLE, SKILL constants
CatalogAccessService.user_roles(user) -> set[str]
CatalogAccessService.grant_level(session, spec, item_id, user) -> str|None
CatalogAccessService.effective_access(session, spec, item, user) -> str|None      # rule P1
CatalogAccessService.accessible(session, spec, user) -> dict[item_id, level]      # 2 queries: grants join + public&listed
CatalogAccessService.can_manage(access, user) -> bool                               # P2
CatalogAccessService.at_least(access, level) -> bool
CatalogAccessService.readers(session, spec, item) -> set[user_id]                  # active users reached via grants
CatalogAccessService.maintainers(session, spec, item) -> list[CatalogMaintainer]
CatalogAccessService.validate_grants(session, spec, item, grants, actor) -> list[row]
CatalogAccessService.replace_grants(session, spec, item, grants, actor) -> (before, after)   # flush, no commit
CatalogAccessService.ensure_maintainer(session, spec, item, user)                    # upsert user/maintain
```
Rules kept from the library:
- Only people can maintain (`"Only a person can be a maintainer."`, 422).
- At least one person keeps `maintain` (409; message `"This item needs at least one maintainer."`).
- An unknown user or role gives 422.
- A duplicate principal folds to its max level.
- Full replace.

### 1.4 Callers rewired
- `CatalogService.list_for_user` (`services/bundles/catalog_service.py:51`), `user_can_see` (:101) and `user_can_install` (:120) go through `effective_access(BUNDLE)`.
- `SkillCatalogService.user_can_see` (`skill_catalog_service.py:236`), `user_can_manage` (:270, which becomes `maintain`), `list_catalog` (:1240), `_entry_context` (:1480) and `user_has_grant` (:1726) go through `effective_access(SKILL)`.
  `can_manage` in `SkillPackageEntry` = maintain. `is_granted` = any grant row.
- `BundleService.create_bundle` (`bundle_service.py:221`) calls `ensure_maintainer` when `publisher_user_id` is set. Ownerless git bundles get no grant.
  `SkillCatalogService._resolve_package` (:791) calls it on create.
- Old grant routes (`routes/bundles.py:270–340` and `routes/skills.py:369–440`): their write paths upsert `user/use` and never downgrade an existing higher level.
  Revoke refuses to remove the last maintainer (409). The authorisation is now "holds `maintain`".
- **Publish needs `edit`.** `PublishService` (`publish_service.py`, where the bundle is resolved near :160) refuses 403 `"You can't publish new versions of this agent."` when the bundle exists and the caller lacks `edit`.
  `SkillCatalogService.publish_from_agent` (:285) changes as follows:
  - when `package_id` names an existing package of **another** publisher and the caller holds `edit`, publish into it;
  - otherwise keep the per-publisher lookup (`_find_publisher_package`, :768);
  - `_apply_publish_to_package` (:1175) grants stay additive and require `maintain` when non-empty.
- `EventType.CATALOG_GRANTED = "catalog.granted"` (meta `{kind:'agent'|'skill', uuid}`) is added in `models/events/event.py` beside :155. It is emitted after commit to `after - before`.
  It lives in `services/catalog/catalog_events.py` (new: `granted(kind, uuid, user_ids)`, the pattern of `library_events.py:97–117`).

### 1.5 Route (new) `backend/app/api/routes/catalog_items.py`, tag `catalog`, prefix `/catalog`
It is registered in `api/main.py` **before** `catalog.router` (:113). In C1 it has only these routes:
- `GET /catalog/items/{uuid}/grants` → `list[CatalogGrant]`. 404 when the caller cannot see the item; 403 when they see it but cannot manage it.
- `PUT /catalog/items/{uuid}/grants`, body `list[CatalogGrantIn]` → `list[CatalogGrant]`. Full replace; emits `catalog.granted`. Audit `CATALOG_GRANTS_CHANGED` (new constant beside `security_event.py:243`).
- Resolution helper `resolve_item(session, uuid) -> (spec, item)`: look in `agent_bundle`, then `skill_package`, else 404 (§0.3‑1).

### 1.6 Checks
`tests/api/agents/bundles/`, `tests/api/agents/core/` (skills catalog files), `tests/api/agents/skill_credentials/`, `tests/api/catalog/`, `tests/migrations/catalog_access_grants_test.py`. Run `make gen-client` and confirm the frontend `tsc` stays clean.

---

## 2. C2: desktop agents are bundles

### 2.1 Models and migration `desktop_bundles`
- `AgentBundle` (`models/bundles/agent_bundle.py:44`) gains:
  - `runs_on: str(16)`, NOT NULL, server default `'cloud'` (`BundleRunsOn.CLOUD|DESKTOP`)
  - `git_repo_id: uuid|None`, FK `git_repo.id` `ondelete=RESTRICT`, unique
  - `manifest_id: uuid|None`
  - `slug: str(64)|None`
  - indexes: partial unique `uq_agent_bundle_desktop_manifest_id(manifest_id)` and `uq_agent_bundle_desktop_slug(slug)` WHERE `runs_on='desktop'`
  - check `ck_agent_bundle_desktop_fields`: `runs_on='cloud' OR (git_repo_id, manifest_id, slug all NOT NULL)`
- `AgentBundleRevision` (`agent_bundle_revision.py:40`) changes:
  - `REVISION_ORIGIN_SHARE='share'` beside :36–37
  - new `git_commit_sha: str(40)|None`
  - `snapshot_path` becomes nullable (it is the only NOT NULL column a share revision cannot fill; `content_hash` = tree id per P8; prompts/SDK are already nullable)
- `GitRepo` (`models/library/git_repo.py:91`) keeps `id, kind, marketplace_id, default_branch, head_sha, last_pushed_by_user_id, manifest_cache, created_at, updated_at`.
  It drops `slug, name, description, owner_user_id` and the indexes `uq_git_repo_agent_slug` and `ix_git_repo_owner_user_id`.
  `GitRepoGrant`, `LibraryPrincipalKind`, `LibraryAccess` and every `Library*` schema are deleted from that file.
- **Minting is shared.** The pure function `BundleIdService.desktop_bundle_id(prefix: str, manifest_id: UUID, taken: set[str]) -> str` returns `<prefix>.<8hex>`, or the first free `-2`, `-3`, … suffix. Both the create route (§2.2) and the migration call it, with `prefix = BundleIdService.reversed_host_prefix()` (host from `settings`, as the route does). The migration imports both, and they import nothing that touches the ORM models at module load. If that import ever breaks, inline an equivalent copy in the migration.
- Upgrade, in order (P7):
  1. Add the bundle columns and indexes, the check, and the revision column; make `snapshot_path` nullable.
  2. Convert each `git_repo` row with `kind='agent'`, ordered by `created_at`, through `op.get_bind()` with plain SQL. For each row:
     - **`manifest_id`** = `manifest_cache->>'id'`, used when it parses as a UUID and no earlier converted row already took it. Otherwise use `uuid4()` and log `catalog_desktop_bundles: repo <id> manifest id missing|duplicate → <new>`. The push policy binds whatever is stored. A repo with a fresh id refuses pushes until the desktop shares it as a new agent; this is noted in `core-status.md`.
     - **`bundle_id`** = `desktop_bundle_id(prefix, manifest_id, taken)`, where `taken` is all existing `agent_bundle.bundle_id` values plus the ids minted in this run.
     - **Bundle row**: `AgentBundle(runs_on='desktop', git_repo_id=repo.id, slug=repo.slug, display_name=repo.name, description=repo.description, publisher_user_id=repo.owner_user_id, is_listed=true, default_install_mode='manual', created_at=repo.created_at, updated_at=repo.updated_at)`.
     - **`visibility`**: `'public'` if the repo had an `everyone` grant; else `'users'` if it had any other grant besides the owner's; else `'private'`.
     - **Grants** go to `bundle_access_grant`. User and role principals map their level `read→use`, `write→edit`, `maintain→maintain`, keeping `granted_by_user_id` and `created_at`. `everyone` grants are dropped, since visibility now carries them. The owner (if not NULL) gets an upsert to `maintain`. If the repo has no maintainer and no owner, log it; a superuser can manage it (P2).
     - **Revision 1** is written only when `head_sha` is set: `AgentBundleRevision(revision_number=1, origin='share', git_commit_sha=head_sha, content_hash=head_sha, snapshot_path=NULL, manifest=manifest_cache or {}, required_credential_specs=[], schedules=[], plugin_specs=[], published_by_user_id=last_pushed_by_user_id, published_at=updated_at, release_notes=NULL)`. Then `latest_revision_id` is set. Specs and `skills_summary` stay empty because the migration does not read git; the next push fills them.
  3. Run `DROP TABLE git_repo_grant`. Drop the `git_repo` columns `slug, name, description, owner_user_id` and their indexes.
- Downgrade (**lossy**; the migration docstring says so):
  1. Re-add the `git_repo` columns as nullable and backfill them from each desktop bundle: `slug`, `name=display_name`, `description`, `owner_user_id=publisher_user_id`. Then restore NOT NULL and the indexes.
  2. Recreate `git_repo_grant` (shape as in `0e62eb9df5d4` and `08bfa6ee8d9f`). Copy bundle grants back with `use→read`, `edit→write`, `maintain→maintain`, and add an `everyone/read` row where `visibility='public'`.
  3. `DELETE FROM agent_bundle WHERE runs_on='desktop'`; revisions and grants cascade. This loses revision history, `bundle_id` and `is_listed`.
  4. Set `snapshot_path` back to NOT NULL and drop the new columns.

### 2.2 Create: `services/catalog/desktop_agent_service.py` (new)
`DesktopAgentService.create(session, user, body: DesktopAgentCreate) -> (AgentBundle, readers)` is a port of `library_service.py:173–238`. In order:
1. `slug` matches `LIBRARY_SLUG_PATTERN` (rename to `KIT_SLUG_PATTERN`, kept in `models/library/git_repo.py`).
2. Slug free among desktop bundles, else 409 `"An agent called {slug} is already in the catalog."`.
3. `manifest_id` free, else 409 `"This agent is already in the catalog. Share changes to it instead."`.
4. Validate grants; the creator becomes `maintain` (folded); people-only maintainers.
5. Mint `bundle_id = BundleIdService.desktop_bundle_id(reversed_host_prefix(), manifest_id, taken)` (§2.1; prefix from `bundle_id_service.py:41`). On collision in `agent_bundle.bundle_id`, it appends `-2`, `-3`, … (still matches `BUNDLE_ID_REGEX`, :31).
6. `GitRepo(kind='agent')` flush → `AgentBundle(runs_on='desktop', publisher_user_id=user, is_listed=True, visibility=body.visibility or 'private', display_name, description, slug, manifest_id, git_repo_id)` → `GitRepoStorage.init_bare` → grant rows → audit `CATALOG_DESKTOP_AGENT_CREATED` → commit.
7. On any failure: rollback and `GitRepoStorage.remove(repo_id)`.
- `DesktopAgentCreate{slug, display_name(1..255), description(≤10k, default ''), manifest_id: UUID, visibility: 'private'|'users'|'public' = 'private', grants: list[CatalogGrantIn] = []}`.
  Who may call: any active signed-in user (U5).
- Delete: `CatalogItemService.delete` (see §2.6) applies the guard. It then deletes the bundle (revisions and grants cascade), deletes the `git_repo` row, and calls `GitRepoStorage.remove` (best effort, as in `library_service.py:296–301`).

### 2.3 Git transport
- `routes/git_http.py:80–95` `_authorize_sync`:
  - resolve `bundle = select(AgentBundle).where(git_repo_id == repo.id)`; missing → 404;
  - `_Authorized` gains `bundle_id: uuid` and `bound_manifest_id: str`.
- `GitHttpService.authorize` (`git_http_service.py:192`) takes `(session, repo, bundle, user, service)`:
  - upload-pack needs `use` or superuser;
  - receive-pack needs `edit` (superusers too);
  - mirrors are still refused.
  The messages stay. The `"Sign in to Cinna to reach the team library."` 401 text becomes `"Sign in to Cinna to reach the catalog."`
- `GitHttpService.spawn_env` (:208) gains `bound_manifest_id`, which sets `CINNA_BOUND_MANIFEST_ID` for receive-pack.
- `push_policy.py`:
  - add `MSG_MANIFEST_ID_CHANGED = "This agent's identity changed; share it as a new agent instead."`;
  - `check_manifest(raw, kit_dir, warn, bound_id=None)` (:341): after schema validation, when `bound_id` is set and `str(data.get("id","")).lower() != bound_id.lower()`, refuse;
  - `evaluate` (:406) passes `environ.get("CINNA_BOUND_MANIFEST_ID")`.
  The policy stays DB-free. `MSG_OTHER_REF` text: "team library" → "catalog".

### 2.4 Revision on push: `services/catalog/desktop_agent_revisions.py` (new; replaces `library_events.py`)
`DesktopAgentRevisions.after_receive(repo_id, before_sha, pusher_id, pushed_new)` is called from `git_http.py:191–199`. It keeps the acceptance test and the CAS from `library_events.py:119–185`. In the same transaction:
1. `SELECT agent_bundle … FOR UPDATE` by `git_repo_id`. `prev = latest revision` (any origin).
2. `release_notes` = `GitRepoStorage.commit_messages(repo_id, prev.git_commit_sha or None, new_sha)` (new helper: `git log --format=%B%x00 [old..]new`, trimmed, joined by blank lines, capped at 10k chars).
3. `GitRepoStorage.export_paths(repo_id, new_sha, ["cinna-agent.json", "skills"], dest=tmpdir)` (new helper: `git archive | tar -x`, same hardened env).
4. Revision fields:
   - `manifest` = `project_revision_manifest(data)` = `{id, name, slug, description, contract_version, runtime:{engine, model}, credentials:[{name,type,service_uri}], catalog_skills:[…as in manifest]}`
   - `required_credential_specs` = `credential_spec.build_spec(session, credential=None, credential_type=c.type, provided_by='user', name=c.name, service_uri=c.service_uri, description=c.description)` per manifest `credentials[]`; a bad entry is logged and skipped
   - `skills_summary` = for each `skills/*/` → `parse_skill_dir` (`services/agents/skill_manifest.py:844`) → `{name, description, has_scripts, source:'authored'}`, plus each `catalog_skills[]` entry → `{name, package_id, package_uuid, revision_number, source:'catalog'}` (name looked up from `skill_package`)
   - `description = manifest.description`
5. Insert `AgentBundleRevision(origin='share', revision_number=max+1, git_commit_sha=new_sha, content_hash=<tree id>, snapshot_path=None, published_by_user_id=pusher_id, release_notes, manifest, required_credential_specs, skills_summary)`.
   Then move `bundle.latest_revision_id`, set `bundle.updated_at`, update `git_repo` as today, audit `CATALOG_DESKTOP_PUSH_ACCEPTED`, commit.
6. After commit, emit `catalog.updated {kind:'agent', uuid, id: bundle_id, revision_number, head_sha}` to `readers` (P9).
- Invariant: a push whose bookkeeping lost the race gets no revision of its own. The next revision's notes cover `prev.git_commit_sha..new`, so no commit message is lost.
- `BundleService.list_revisions_with_install_counts` (`bundle_service.py:198`) and `delete_revision` (:401) filter `origin IN ('publish','share')`.

### 2.5 Cloud paths refuse desktop bundles (409 `{"code":"desktop_only","message":"This agent runs on your computer in Cinna Desktop."}`)
New `DesktopOnlyError` in `services/bundles/exceptions` and a helper `BundleService.require_cloud(bundle)`. Call sites:
- `routes/catalog.py`: `GET /{bundle_id}` (:41), `install-context` (:53), `install` (:73), `admin-install` (:97).
- `InstallService._install_from_revision` (`install_service.py:312`, the choke point for install, admin install, channel auto-install and `/agents/checkout` via `git_source_service.py:427`) and `apply_update` (:960).
- `CatalogService.user_can_install` returns False for desktop. Channel Pass 2 (`channel_routing_service.py:2579`) then records `not_installable`.
- `routes/bundles.py`: GET/PATCH/DELETE `/{bundle_uuid}`, revisions and grants on a desktop bundle → 409. `GET /bundles/` and `GET /catalog/` filter `runs_on='cloud'` (P5).
- `PublishService`: first publish whose `Agent.bundle_id` equals an existing desktop bundle's id → 409 `desktop_only`.
- `POST /agents/checkout` with a URL where `GitHttpService.parse_repo_file`/`repo_id_in_url` resolves to a core repo → 409 `desktop_only`, before cloning. Move `repo_id_in_url` from `library_service.py:89` to `git_repo_storage.py`.

### 2.6 Catalog API (all of §5.1 except local installs): `routes/catalog_items.py` + `services/catalog/catalog_item_service.py`
| Route | Body → response | Gate |
|---|---|---|
| `GET /catalog/items?kind=&runs_on=&scope=` | → `CatalogItemsPublic{data: CatalogItem[], count}` | signed in. `scope=all` superuser only (else 403) |
| `GET /catalog/items/{uuid}` | → `CatalogItemDetail` = `CatalogItem` + `revisions: CatalogRevision[]` | `use` (else 404) |
| `POST /catalog/desktop-agents` | `DesktopAgentCreate` → `CatalogItem` | signed in |
| `PATCH /catalog/items/{uuid}` | `CatalogItemUpdate{display_name?, description?, visibility?, is_listed?}` → `CatalogItem` | P2 |
| `GET/PUT /catalog/items/{uuid}/grants` | from C1 | P2 |
| `DELETE /catalog/items/{uuid}` | 204; 409 `"{n} person/people use this. It can be removed once nobody does."` | P2 |

- `CatalogItem` and `CatalogRevision{revision_number, version, published_at, published_by, release_notes, origin, git_commit_sha?}` are in `models/catalog/catalog_item.py`, exactly as §5.1 plus §0.3‑2/3.
- `CatalogItemService.list(session, user, kind, runs_on, scope)` builds `accessible(BUNDLE)` ∪ `accessible(SKILL)`. Ownerless **cloud** bundles are excluded (`publisher_user_id IS NULL AND runs_on='cloud'`). A migrated desktop bundle whose owner is gone stays listed. P4 applies.
  Batched queries for maintainers, publishers, latest revisions, install counts and the caller's cloud installs. No N+1.
- Fields:
  - `my_installs` (C2: cloud only): `Agent` rows of the caller with `bundle_uuid`, and `AgentPluginLink` rows on the caller's agents for the package.
  - `install_count`: the existing per-kind people counts (`BundleService.install_count`, skill entry count), extended in C3.
  - `desktop{repo_id, clone_url, default_branch, head_sha, manifest_id, slug, engine, contract_version}`: from `git_repo` + `manifest_cache`; `clone_url` from `library_service.py:85`, moved to `GitRepoStorage.clone_url`.
  - `skill{frontmatter, archive_sha256, size_bytes}`: from the latest revision.
  - `skills[]` for agents: from `skills_summary`; cloud bundle entries are `source:'authored'`.
- `PATCH` on a skill behaves like today's `PATCH /skills/packages/{id}`. A maintainer may re-list. This is the existing gap, see risk 8.
- `DELETE` per kind:
  - cloud bundle → `BundleService.delete_bundle` (`bundle_service.py:455`);
  - desktop → §2.2;
  - skill → new `SkillCatalogService.delete_package` (P12: count other people's links; remove storage dir after commit).
  Audit `CATALOG_ITEM_DELETED`.

### 2.7 Removal list
- Backend:
  - `api/routes/library.py` and its line in `api/main.py:143`;
  - `services/library/{library_service,library_access_service,library_events}.py`;
  - `EventType.LIBRARY_UPDATED/LIBRARY_GRANTED` (`event.py:151–156`) → `CATALOG_UPDATED = "catalog.updated"` and the C1 `CATALOG_GRANTED`;
  - audit `LIBRARY_*` (`security_event.py:243–246`) → `CATALOG_DESKTOP_AGENT_CREATED`, `CATALOG_DESKTOP_PUSH_ACCEPTED`, `CATALOG_GRANTS_CHANGED`, `CATALOG_ITEM_DELETED`;
  - `services/library/exceptions.py` keeps only what the transport uses (`LibraryAccessDeniedError` → `GitAccessDeniedError`).
- Tests:
  - delete `tests/api/library/library_api_test.py`;
  - move `library_git_http_test.py` → `tests/api/catalog/catalog_desktop_git_test.py` (create through `/catalog/desktop-agents`), `library_push_policy_test.py` → `tests/api/catalog/catalog_desktop_push_policy_test.py`, and `library_events_test.py` → covered by `catalog_events_test.py`;
  - move the domain `conftest.py` seam to `tests/api/catalog/conftest.py`, patching `desktop_agent_revisions.Session`;
  - update `tests/utils/library.py` (rename to `tests/utils/catalog.py`);
  - update `tests/unit/test_library_models_and_storage.py` for the dropped columns;
  - `tests/migrations/git_repo_library_test.py` stays, because it pins its own revisions.
- Frontend: delete `routes/_layout/admin/team-library.tsx`, `components/Admin/TeamLibrary/*` and the menu entry `components/Sidebar/AdminMenu.tsx:70`. Run `make gen-client`; `routeTree.gen.ts` regenerates. C3 rebuilds People from `git show d5bb0a16:frontend/src/components/Admin/TeamLibrary/{LibraryPrincipalPicker,LibraryGrantRow,LibraryAccessForm}.tsx`.
- `core-status.md`: "commit <C2> removes `/api/v1/library*` and `library.*` events".

### 2.8 Checks
- `tests/api/catalog/`, `tests/api/agents/bundles/`, `tests/api/agents/bundles_install/`, `tests/api/agents/git/`, `tests/api/server_channels/` (Pass 2), `tests/unit/test_push_policy_bound_manifest_id.py`, `tests/unit/test_desktop_bundle_id.py`.
- **Manual migration check** (the conversion has no API to drive it, and no test file is written for it):
  1. On a dev DB at C1 head, create two library agents through `/library` before C2: one with an everyone grant and a push, one private with a role grant and no push.
  2. Give a third repo a `manifest_cache.id` that duplicates the first.
  3. Run `alembic upgrade head` and check: bundles minted; visibility public/private/users; levels mapped; owner is maintain; revision 1 only for the pushed repo; the duplicate got a fresh `manifest_id` and a log line; a `git clone` and a push with the bound id still work.
  4. Run `alembic downgrade -1`, then `upgrade head`, both clean. Record the result in `core-status.md`.
- `make check-docs` (route citations).

---

## 3. C3: unified listing, local installs, events, web

### 3.1 Model and migration `catalog_local_install`
`models/catalog/catalog_local_install.py`, table `catalog_local_install`:
- `id` uuid PK
- `user_id` FK user CASCADE
- `device_id` str(128)
- `item_kind` str(16) (`agent`|`skill`)
- `item_uuid` uuid (no FK: two target tables; rows are removed by the item delete service)
- `revision_number` int
- `host_manifest_id` uuid NULL (skills only)
- `created_at`, `updated_at`
- unique `uq_catalog_local_install(user_id, device_id, item_kind, item_uuid, host_manifest_id)` with `postgresql_nulls_not_distinct=True` (compose runs PG17)
- index `(item_kind, item_uuid)`

Downgrade drops the table.

### 3.2 Service and routes: `services/catalog/local_install_service.py`
- `POST /catalog/items/{uuid}/local-installs` with `LocalInstallIn{device_id, revision_number, host_manifest_id?}` → `CatalogItem`.
  - Gate: `use`, or (for skills) the U7 rule of §4.3.
  - Validation: the revision exists; a skill needs `host_manifest_id` and an agent refuses one (422); a cloud bundle → 409 `desktop_only` inverse (`"This agent runs in Cinna."`).
  - Upsert moves `revision_number`.
- `DELETE /catalog/items/{uuid}/local-installs/{device_id}?host_manifest_id=` → 204. Idempotent; caller's rows only.
- `install_count` = distinct people (excluding the publisher, as today) over cloud installs ∪ local rows. `my_installs` adds `{where:'local', device_id, host_manifest_id, revision_number}`.
- The delete guard (§2.6) counts other people's local rows. Item delete removes its local rows.

### 3.3 Events
`catalog.updated` is also emitted:
- by `PublishService` after a bundle revision commit (next to the existing `BUNDLE_PUBLISHED`);
- by `SkillCatalogService.publish_from_agent`, after `_apply_publish_to_package`.

Readers = P9 ∪ users holding cloud or local installs. Meta as §5.3, with `head_sha` only for desktop.

### 3.4 Frontend (UI Specification to be added by `cinna-core-ui-designer`)
- Run `make gen-client` first. Data: `CatalogService.listCatalogItems`, `getCatalogItem`, `updateCatalogItem`, `getCatalogItemGrants`, `replaceCatalogItemGrants`, `deleteCatalogItem`. Query keys `["catalog-items", filters]` and `["catalog-item", uuid]`.
- Socket `catalog.updated`/`catalog.granted` invalidate `["catalog-items"]`.

Surfaces (intent, data and actions only; composition is the designer's):
| Surface | Intent | Data | Actions |
|---|---|---|---|
| S1 Catalog list (`routes/_layout/catalog/index.tsx`) | find any item | `CatalogItem[]`: kind, runs_on badge (*Runs in Cinna* / *Runs on your computer*), display_name, description, publisher, latest version + date, install_count, needs (credential specs), my install state | kind filter (all / cloud agents / desktop agents / skills); existing filters (public / shared / installed) still apply; open the item; for a cloud agent Install (existing install route); for a skill Add to agent (existing `AddSkillToAgentDialog`) |
| S2 Desktop agent detail (new route, e.g. `catalog/items/$uuid`) | understand a desktop agent before getting it on the desktop | versions list (`revisions`: Version N, shared by, date, release notes), skills, needs, engine | no install button; optional `cinna://catalog/<id>` link; maintainer actions (S3) |
| S3 Maintainer controls (on S2, on the existing skill page `catalog/skills/$packageId.tsx`, and on the cloud agent's catalog detail) | manage who can use or change the item | grants (`CatalogGrant[]`), visibility, is_listed | People editor (principal picker: user or role; levels Can use / Can share changes / Maintainer, people only); change visibility; delete (shows the 409 sentence) |
| S4 Admin catalog management (replaces `/admin/team-library`; menu entry in `AdminMenu.tsx`) | superuser oversight of every item | `listCatalogItems({scope:'all'})` | the S3 actions on any item; existing skill delist |

- `CatalogSectionTabs.tsx` (Agents / Skills tabs) folds into S1's kind filter. `catalog/skills/index.tsx` redirects to `/catalog?kind=skill`.
- `SkillPackageAccessCard`/`AddSkillPackageGrantDialog` move to the S3 People editor (role grants and levels).

### 3.5 Checks
- `tests/api/catalog/`, `tests/api/agents/core/` (skills catalog), `tests/api/agents/bundles/`.
- Frontend: `cd frontend && npx tsc --noEmit 2>&1 | grep -E "Catalog|AdminMenu"`; `docker compose build frontend` before any screenshot (5173 is an nginx build).

---

## 4. C4: skills published from a desktop

### 4.1 Split the publish core (`skill_catalog_service.py:285–545`)
Extract `_publish_tree(session, *, user, skill_dir, package|None, entry, version, release_notes, visibility, grants: list[CatalogGrantIn], credential_specs, source_agent_id|None) -> SkillPackageRevision`.
- It covers steps 5–7 of the current function: lock, version resolution, version write-back into `skill_dir/SKILL.md`, snapshot plus deterministic archive, the revision row, and `_apply_publish_to_package` with `agent` made optional (`source_agent_id` is left unchanged when None).
- `publish_from_agent` keeps its own steps 1–4 and calls it. Behaviour is unchanged; the existing tests must pass untouched.

### 4.2 Upload: `services/skills/skill_upload.py` (new) + routes in `routes/skills.py` (`router`, declared before `/packages/{package_id}` routes)
- `POST /skills/packages/publish-preview`, body `SkillDesktopPublishPreviewIn{name, frontmatter: dict, package_uuid?: UUID}` → `SkillPublishPreview` (existing schema).
  - `package_uuid` given: needs `edit` (else 404/403).
  - Version from `resolve_version` (:965). `package_id` from `derive_package_id` (:895) on first publish.
  - Credentials parsed from `frontmatter.credentials` via `parse_credential_declarations` (`skill_manifest.py:632`), all `provided_by='user'`.
- `POST /skills/packages/publish`, multipart `archive: UploadFile` + `meta: str` (JSON `SkillUploadMeta{package_uuid?, version?, release_notes?, visibility?, grants?: CatalogGrantIn[]}`) → `SkillPackageRevisionPublic`. Steps in order (nothing is written before step 4):
  1. Read the stream with caps: compressed bytes ≤ `MAX_PACKAGE_BYTES` (16 MiB), else 413 `skill_too_large`.
  2. `safe_extract(tar.gz, tmpdir)`. Refuse 400 `skill_archive_invalid` on any of: absolute path, `..`, symlink/hardlink/device/fifo, more than one top-level dir, uncompressed total > `MAX_PACKAGE_BYTES`, member count > 2000.
  3. `parse_skill_dir(tmpdir/<name>)`. Refusals: `skill_invalid` (incl. folder ≠ frontmatter name), `skill_contains_secrets`, `skill_too_large`. These are the same codes and sentences as the cloud path (`STATUS_BY_CODE`).
  4. Package resolution:
     - with `package_uuid` → package exists, caller `edit`, `package.name == entry.name` (else 409 `skill_name_mismatch`);
     - without it, the caller already publishes a package of that name → 409 `package_exists` + `package_uuid` (§0.3‑4);
     - otherwise create via `_resolve_package` with `agent=None` (signature change: `source_agent_id=None`). The creator becomes `maintain`.
  5. `visibility` or `grants` on an existing package need `maintain`. `use` grants under a non-`users` effective visibility → 409 `grants_require_users_visibility` (existing rule).
  6. `credential_specs` = `build_spec(credential=None, provided_by='user', …)` per declaration.
  7. `_publish_tree`. Version write-back goes into the tmp copy, so the snapshot carries it. The response `version` is what the desktop writes back.
- Who may call: any active user (U5).

### 4.3 Download through an agent (U7): `routes/skills.py:223`
When `user_can_see` fails, allow when `SkillCatalogService.pinned_by_usable_desktop_agent(session, package, revision_number, user)`. That holds when some desktop bundle the caller can `use` has a latest revision whose `manifest.catalog_skills[]` contains `{package_uuid == package.id, revision_number == n}`.
- Implementation: `accessible(BUNDLE, user)` filtered to `runs_on='desktop'` → their latest revisions → check in Python.
- The same rule gates `content`/`files` reads. The skill's `GET /catalog/items/{uuid}` does **not** open through U7 (download only).

### 4.4 Events and counts
- `catalog.updated {kind:'skill'}` on upload publish (the same call as §3.3).
- `install_count` already includes local rows (§3.2).

### 4.5 Checks
`tests/api/catalog/`, `tests/api/agents/core/agents_skills_*`, `tests/api/agents/skill_credentials/`.

---

## 5. C5: skill credentials for local installs

- `CredentialProvisioner.provision_local(session, *, user, specs, publisher: PublisherBoundary) -> list[SlotOutcome]` (`credential_provisioner.py`, after :222).
  - For each `provided_by='publisher'` spec, re-check the publisher still owns the credential, `allow_sharing`, `allow_local_use` (`models/credentials/credential.py:46`), type and slot.
  - Then upsert `CredentialShare(source='skill_local_install')` for `user`. There is no agent and no link.
  - Outcomes: `shared | already_shared | not_allowed_locally | not_shareable | user_provides`.
- `DesktopCredentialService` (`desktop_credential_service.py:201–231`) treats `share.source == 'skill_local_install'` as deliverable locally.
- Route `POST /skills/packages/{uuid}/revisions/{n}/provision-local` → `SkillLocalProvisionResult{slots: SlotOutcome[]}`. Gate: `use` or U7. Idempotent.
- Desktop publishes may now resolve `publisher`/`template` the same way the cloud path does, but against the publisher's own credentials matching `(type, service_uri=slot)` instead of credentials linked to an agent (`SkillCredentialRequirements.resolve_for_publish`, with a new `agent=None` branch).
- Checks: `tests/api/catalog/`, `tests/api/credentials/`, `tests/api/external/` (desktop credentials).

---

## 6. Tests checklist (by file; core.md "Tests to write" → phase)

**C1**
- `tests/migrations/catalog_access_grants_test.py`
  - [ ] seeded bundle/skill grants become `user/use`
  - [ ] publisher gets `user/maintain`
  - [ ] a publisher who already had a grant row gets one row at maintain
  - [ ] downgrade restores the old rows
- `tests/api/catalog/catalog_access_test.py`
  - [ ] `use` under private is invisible
  - [ ] a role grant (`agent-user`) shows a `users` item to that role
  - [ ] the `admin` role matches a superuser
  - [ ] delisting hides it from `use` but not from `edit`
  - [ ] bundle publish without `edit` gives 403
  - [ ] skill publish into another person's package with `edit` gives 200, with `use` gives 403
- `tests/api/catalog/catalog_grants_test.py`
  - [ ] GET/PUT maintain only (404/403)
  - [ ] role cannot maintain (422)
  - [ ] last maintainer (409)
  - [ ] `catalog.granted` to newly reached users only
  - [ ] old `POST /bundles/{uuid}/grants {email}` and `POST /skills/packages/{id}/grants` still work and never downgrade

**C2**
- `catalog_desktop_agents_test.py`
  - [ ] create gives a minted `bundle_id`, Version none, visible to creator only
  - [ ] duplicate slug (409)
  - [ ] duplicate manifest id (409)
  - [ ] `bundle_id` collision suffix
  - [ ] PATCH
  - [ ] DELETE moves the repo
  - [ ] superuser `scope=all`
- `catalog_desktop_git_test.py` (real git)
  - [ ] push → revision 1 with commit-message notes, specs from `credentials[]`, `skills_summary` (authored + catalog)
  - [ ] second push → revision 2 notes only since 1
  - [ ] the `use` grantee clones but cannot push
  - [ ] `edit` pushes
  - [ ] superuser clones
- `catalog_desktop_push_policy_test.py`
  - [ ] every existing refusal sentence
  - [ ] manifest id change refused
- `tests/unit/test_push_policy_bound_manifest_id.py`
  - [ ] equal / different / case / missing id
- `catalog_desktop_only_guards_test.py`
  - [ ] install, install-context, admin-install, `GET /catalog/{bundle_id}`, `/bundles/{uuid}*` → 409 `desktop_only`
  - [ ] old `GET /catalog/` excludes desktop
  - [ ] checkout of a core URL → 409
- `catalog_items_test.py`
  - [ ] route order: `GET /catalog/items` is a list and `GET /catalog/items/{uuid}` an item, not `/catalog/{bundle_id}`
  - [ ] `kind`/`runs_on` filters
  - [ ] detail revisions
- `tests/unit/test_desktop_bundle_id.py`
  - [ ] `<prefix>.<8hex>`
  - [ ] `-2`/`-3` suffixes against `taken`
  - [ ] matches `BUNDLE_ID_REGEX`
- Migration conversion: manual alembic up/down check (§2.8), not a test file

**C3**
- `catalog_listing_matrix_test.py`
  - [ ] visibility matrix across kinds (cloud, desktop, skill) × (private/users/public × listed) × roles × levels
  - [ ] P4
- `catalog_local_installs_test.py`
  - [ ] upsert and delete
  - [ ] skill needs `host_manifest_id`
  - [ ] counts across cloud + local
  - [ ] **delete guard with a local install** (agent and skill)
  - [ ] `my_installs`
- `catalog_events_test.py`
  - [ ] `catalog.updated` on bundle publish, desktop push, skill publish (rooms)
  - [ ] `catalog.granted`

**C4**
- `catalog_skill_upload_publish_test.py`
  - [ ] valid upload (version returned, archive sha, specs `user`)
  - [ ] invalid SKILL.md
  - [ ] secret file
  - [ ] oversize (compressed and uncompressed)
  - [ ] unsafe archive (`..`, symlink, two roots)
  - [ ] **edit-only republish** (editor ok, `use` 403)
  - [ ] `package_exists` 409
  - [ ] name mismatch
  - [ ] preview
- `catalog_skill_download_via_agent_test.py`
  - [ ] **download via agent reference** (no grant on the skill, `use` on a desktop agent pinning rev n) gives 200
  - [ ] other revision gives 404
  - [ ] agent access revoked gives 404

**C5**
- `catalog_skill_provision_local_test.py`
  - [ ] the share appears in `/external/credentials`
  - [ ] `allow_local_use=false` → `not_allowed_locally`
  - [ ] idempotent

## 7. Docs (per phase; then `make docs-registry` and `make check-docs`)
- **C1**
  - `docs/agents/agent_bundles/agent_bundles.md` and `_tech.md` § access: "Access is visibility plus grants (user or role × use/edit/maintain); publishing a new version needs edit."
  - `docs/agents/agent_skills/agent_skills.md` and `_tech.md` § visibility: the same sentence, plus "an admin can still delist".
  - `tests/api/catalog/README.md` (new).
- **C2**
  - `git mv docs/application/team_library/team_library*.md docs/application/catalog/desktop_agents*.md` and rewrite the opening: "A desktop agent is a catalog bundle with `runs_on='desktop'` whose content store is a git repo; every accepted Share changes is a version." Frontmatter `feature: desktop_agents`.
  - Remove the `/library` routes and events from the tech doc; document the bound manifest id and the share revisions.
  - `docs/agents/agent_bundles/agent_bundles_tech.md`: `runs_on`, `origin='share'`, `desktop_only`.
  - `docs/application/realtime_events/event_bus_system.md`: `catalog.updated`, `catalog.granted` replace `library.*`.
- **C3**
  - New `docs/application/catalog/catalog.md` and `catalog_tech.md` (frontmatter `feature: catalog`, `affects: [agent_bundles, agent_skills, desktop_agents]`): one listing, kinds, access model, local installs, delete guard, events, the web page and the admin view.
  - `docs/application/local_agent_kit/local_agent_kit.md` § sharing: points at the catalog.
- **C4**
  - `docs/agents/agent_skills/agent_skills.md` § publish: "Skills can also be published by upload from Cinna Desktop; credential slots are user-provided until provision-local."
  - `catalog.md` § U7.
- **C5**
  - `docs/application/desktop_credentials/desktop_credentials.md`: "Shares created by provision-local are delivered locally."
  - `agent_skills_tech.md`.

## 8. Risks
1. **Access regression on released data (C1).** Bundle `user_can_see` had no superuser rule and skills had a special one; P1 unifies them. The matrix test must pin the old answers for publisher, grantee, stranger and superuser before the code changes.
2. **`publisher_user_id` RESTRICT** on `agent_bundle`: any user who shares a desktop agent can no longer be deleted. That is the existing bundle behaviour, now reachable by agent-users. Check the user-delete path, and if needed null the publisher for desktop bundles that still have another maintainer.
3. **Revision bookkeeping is a background task.** A crash after the push but before the insert leaves `head_sha` ahead of `latest_revision`. The next push heals it (notes range from `prev.git_commit_sha`). Nothing heals it without a next push; a later sweep could.
4. **U7 checks the latest revision only.** A teammate on an older agent version whose old pin was replaced loses the download until they update.
5. **Route shadowing.** `/catalog/items` must be registered before `/catalog/{bundle_id}`, and `/skills/packages/publish*` before `/skills/packages/{package_id}`. Both are pinned by tests.
6. **Desktop break window.** `/library` disappears in C2. The desktop's live team-sharing specs (D2) must move in the same window; `core-status.md` names the commit.
7. **Converted repos with a fresh `manifest_id`** (missing or duplicate id) refuse the desktop's next push with `MSG_MANIFEST_ID_CHANGED`. Dev desktops affected share them as new agents. The migration log names each one.
8. **The admin delist lever versus maintainers re-listing** (pre-existing). There is no `delisted_by_admin` column, so a maintainer can re-list a skill an admin delisted. This plan accepts the gap and notes it in `catalog.md`. Fixing it needs a column (a later change).

---

## UI Specification

_Produced by cinna-core.ui.design on 2026-10-10 for the C3 frontend (§3.4). Composition decisions here override §3.4's surface table; data and API decisions stay with the plan. Guideline: `docs/development/frontend/ui_ux_guidelines.md`._

**Vocabulary (U12, applies to every surface):** kind words *Runs in Cinna* (cloud agent) / *Runs on your computer* (desktop agent) / *Skill*; kind glyphs `Bot` / `Laptop` / `GraduationCap`, the same in tile, detail, Sheet and admin table (§2 The same entity, before and after). Version label = one helper `catalogVersionLabel(latest)`: `latest.version ? "v" + version : "Version " + revision_number` (`||` on the wire string, §2 Wire strings), used by S1, S2, S4 and the install header card. Access levels *Can use* / *Can share changes* / *Maintainer*. **No git words anywhere:** no commit, sha, branch, clone URL, push or repository is rendered (so `git_commit_sha`, `desktop.clone_url`, `default_branch`, `head_sha` are never shown, not even in `RowInfo`).

### Surfaces

| # | Surface | Host | Story | Placement (§2 row) | Pattern | Budget | Verification |
|---|---------|------|-------|--------------------|---------|--------|--------------|
| S1 | Catalog page (all kinds) | `routes/_layout/catalog/index.tsx` | Manage list (browse) | route; toolbar per §2 Page toolbar | existing catalog tile grid (`CatalogCard`/`SkillCatalogCard` merged) + segmented control + `Filters` menu | toolbar 2 controls; tile ≤ 7 elements, ≤ 2 badges, 1 footer action | checklist + screenshots |
| S1a | Catalog item tile | S1 grid | View | tile, `auto-rows-fr` grid | as above | 7 elements; 1 action | (with S1) |
| S2 | Desktop agent detail | new route `routes/_layout/catalog/items/$uuid.tsx` | View | route, two columns (host shape of `catalog/skills/$packageId.tsx`) | skill-package detail host: facts card + document card; history off the Version fact (§2 A card under a document) | left card 3 blocks (facts ≤ 9 lines, id, button); right card 1 block | checklist + screenshots |
| S2a | Versions Sheet | S2, opened from the Version fact | View | Sheet (§5) | P5 Sheet, `SkillRevisionRow` shape | all revisions; 0 actions, row is the control | checklist |
| S2b | Skills Sheet / Credentials Sheet | S2, opened from facts | View | Sheet | `SkillRevisionFilesSheet` / `SkillRevisionCredentialsSheet` shape | read-only rows | checklist |
| S3a | People dialog | opened from S3c on every host and from S4 | Edit | Dialog, one section (§1 Edit, §2 Disclosure depth) | rebuilt `LibraryAccessDialog` + `LibraryAccessForm` + `LibraryGrantRow` + `LibraryPrincipalPicker` (`git show d5bb0a16:…`) | 1 field + 1 list + picker; rows: 1 Select + 1 remove | checklist |
| S3b | Delete confirm | from S3c / S4 | Edit (destructive) | `AlertDialog` with refusal Alert (§2 Confirmation) | rebuilt `RemoveLibraryAgentDialog` | 1 destructive button | checklist |
| S3c | Maintainer menu | header `⋯` of: S2 facts card, `SkillPackageCard`, `InstallAgentHeaderCard` | Edit (entry point) | `RowActionsMenu` on the card title row (§2 Row actions) | P3 menu | ≤ 4 items | checklist |
| S4 | Admin catalog | new route `routes/_layout/admin/catalog.tsx`, replaces `/admin/team-library`; `AdminMenu.tsx` entry | Manage list | route (§1 Manage list) | P9 | 7 columns + `⋯`; menu ≤ 5 items | checklist + screenshots |

### S1 — Catalog page

- **Intent:** "Show me everything I can get here, narrowed to one kind when I know what I want."
- **Layout:** the current `catalog/index.tsx` host (`p-6 md:p-8`, `max-w-7xl`, one `justify-between` toolbar row, then the grid `grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 auto-rows-fr`). Header title "Catalog", subtitle "Agents and skills shared on this instance".
- **Toolbar (§2 Page toolbar, R22):** left, `CatalogKindTabs` — the `CatalogSectionTabs` segmented control (same track and `bg-primary` on-state) re-pointed at the `?kind=` search param, 4 segments: *All · Agents in Cinna · Agents on your computer · Skills* (each with its kind glyph). Kind is the successor of the two section tabs and changes what the tiles' action is, so it keeps the navigation vocabulary; it is a `nav`-less segmented control with `role="radiogroup"` + `role="radio"`/`aria-checked` because it no longer changes route (§2 Choice-control ARIA). Right, one `CatalogFilterMenu` with options *All · Public · Shared with me · Installed · Mine* (Mine = `access` is `edit` or `maintain`), active subset on the trigger. Both selections live in the URL search (`kind`, `filter`).
- **Tile (S1a), one component `CatalogItemCard`** replacing `CatalogCard` + `SkillCatalogCard` (their header skeleton: muted icon tile + `CardTitle text-lg` + "by <publisher.display_name>"). 7 elements in order: (1) header with kind glyph; (2) description `line-clamp-3`; (3) badge row — agents: kind badge (*Runs in Cinna* / *Runs on your computer*) + version badge; skills: version badge only (the glyph already says skill) — then the date as muted `RelativeTime` of `latest.published_at`; (4) needs line `KeyRound` + "Needs Odoo, IMAP" (`required_credential_specs`), only when non-empty; (5) agents only: skills line (`GraduationCap`, ≤ 3 names + "+N more", the current `CatalogCard` rule); (6) my-install line, only when `my_installs` is non-empty: cloud agent "Installed" / "Installed · update available" (derived: cloud install `revision_number` < `latest.revision_number`), desktop agent "On N of your computers", skill "Used in N of my agents"; (7) footer, one action. Dropped from the tile: the visibility badge (the filter answers it; S2/skill page state it), the id `<code>` chip (detail only), publisher email lines.
- **Footer action per kind:** cloud, not installed → *Quick Install* (pending "Installing…"); installed with update → *Update* (`variant="default"`, pending "Updating…" — **not** `bg-amber-500`, R14a); installed → *Open* (no `ExternalLink` glyph, A12); desktop → *Open in Cinna Desktop* (`<a href="cinna://catalog/<id>">` as an outline button, `Laptop` icon, same label whatever the install state, §6); skill → *Add to agent* (`AddSkillToAgentDialog`, unchanged).
- **Tile click:** cloud → install route (or the agent when installed, as today); desktop → `/catalog/items/$uuid`; skill → `/catalog/skills/$packageId`.
- **Interaction model:** none (navigation + one action per tile).
- **States:** loading 8 `Skeleton h-[240px] rounded-xl` tiles (the skills route's; `PendingItems` table skeleton removed); error `QueryErrorAlert` "Couldn't load the catalog" + Retry, gated `isError && !data`; empty, unfiltered, per kind — All: "Nothing is shared with you yet." ; Agents in Cinna: "Publish one from an agent's Bundle tab." linking `/agents`; on your computer: "Share one from Cinna Desktop."; Skills: the current skills copy with its link. Filtered empty: "Nothing matches this filter." + link button "Show everything" that resets `filter`. Pending: per tile on its footer button only.
- **Facts & values:** one timestamp per tile → `RelativeTime`; no links in the tile body; no copy affordance.
- **Components:** `CatalogKindTabs` (rewrite of `CatalogSectionTabs`), `CatalogFilterMenu` (unchanged) with one option list `CatalogItemFilters` replacing `CatalogFilters` + `SkillCatalogFilters`, `CatalogItemGrid` replacing both grids, `CatalogItemCard`, `QueryErrorAlert`, `RelativeTime`. `catalog/skills/index.tsx` becomes a redirect to `/catalog?kind=skill`; the skill page's and install route's `goBack` fallbacks become `/catalog?kind=skill` and `/catalog`.
- **Anti-patterns:** in `CatalogCard.tsx` (deleted into `CatalogItemCard`): raw `bg-amber-500` (R14a) fixed; `ExternalLink` on an internal Open fixed (A12); error text without Retry and the table skeleton on a grid page fixed (§6).
- **Screenshots:** `/catalog` at 1440 and at 1024 (toolbar on one row at 1024, mixed-kind tiles at equal height). If the dev DB has no desktop agent: review that tile from JSX, "required but not capturable" for it.

### S2 — Desktop agent detail (`/catalog/items/$uuid`)

- **Intent:** "Before I get this agent on my computer: what is it, who shares it, what changed lately, what does it need?"
- **Layout:** copy the host of `routes/_layout/catalog/skills/$packageId.tsx`: `max-w-6xl`, `grid items-start gap-6 lg:grid-cols-[minmax(280px,360px)_1fr]`, back button + title (`display_name`) + "by <publisher>" in the page header. The route serves `runs_on='desktop'` only; for any other kind it redirects (cloud → install route, skill → skill page), so one entity has one detail page.
- **Left card "Agent"** (shared card skeleton, icon `Laptop` `h-5 w-5`, title "Agent", `CardDescription` = description or "This agent has no description."; S3c `⋯` on the title row for `access='maintain'` or superusers). Blocks: (1) fact list (label noun / value with unit, §2 Fact labels): *Publisher*; *Where it runs* "On your computer"; *Version* — `catalogVersionLabel` of the shown revision as a door (button + `History` glyph) to S2a, or "Nothing shared yet" when there is no revision (P4); *Engine* `desktop.engine` (row omitted when empty, `||`); *Skills* "3 skills" door to S2b, or "None"; *Credentials* "Odoo, IMAP" door to the credentials Sheet, or "None declared"; *Catalog installs* "N people"; *My computers* "N computers" (only when > 0); *Access* visibility label (+ ", delisted" when `!is_listed`). (2) *Catalog id* `id` as a click-to-copy `font-mono` button, tooltip "Click to copy", toast "Catalog id copied" (§2 Copying a visible value — not `CopyableValue`). (3) primary `Button className="w-full"` *Open in Cinna Desktop* (`cinna://catalog/<id>`), with one muted line under it: "Needs Cinna Desktop on this computer." No install button.
- **Right card "Release notes"** (icon `FileText`, title "Release notes", description "<Version label> · shared by <published_by> · <RelativeTime>"): the shown revision's notes as `text-sm whitespace-pre-wrap break-words` (they are commit-message prose, not markdown — do not run `MarkdownRenderer`). Empty notes: "No notes were written for this version." No revision: "Nothing shared yet. Versions appear here after the first Share changes from Cinna Desktop." Only card in this column (§2 A card under a document).
- **S2a Versions Sheet:** the `AllSkillRevisionsSheet` + `SkillRevisionRow` shape (generalise them over `CatalogRevision`, do not fork): every revision, newest first; row = `ListRow`, status dot `on` with label "Latest version" on the latest only, title = version label, meta "shared by <name> · <RelativeTime>", release notes as the `FileText` `RowFlag`; the **row is the control** (§2 Row as the control: no radius, portal guard, `focus-visible:ring-inset`) and pins the right card to that revision; 0 actions, no `⋯`. Sheet list `pr-2`.
- **S2b Skills Sheet:** read-only `ListRow`s: glyph `GraduationCap`, title = skill name, one `Badge h-5` "From the catalog" on `source='catalog'` rows only (authored is the default, absent fact renders nothing), description in `RowInfo`. No links (the payload has `package_id`, not the uuid the skill route needs — see Open questions). **Credentials Sheet:** reuse `SkillRevisionCredentialsSheet` generalised to take `CredentialSpec[]`.
- **Interaction model:** read-only; maintainers write only through S3.
- **States:** loading two `Skeleton` cards (340 / 320 px, as the skill page); error `QueryErrorAlert` "Couldn't load this agent" + Retry, with the skill page's "if it was made private…" line linking `/catalog?kind=desktop`; 404 reads the same. Socket `catalog.updated` for this uuid invalidates `["catalog-item", uuid]`.
- **Facts & values:** timestamps → `RelativeTime` (card description, Sheet rows); links: none in facts (the desktop link is a button whose label says where it goes, no glyph needed); click-to-copy: catalog id only; rendered documents: none; Sheets scroll natively.
- **Components:** `Card`, `ListRow`/`ListRowGroup`/`RowFlag`/`RowInfo`, `Sheet`, `RelativeTime`, `QueryErrorAlert`, `Skeleton`, `Tooltip`. Header icons: `Laptop`, `FileText`.
- **Siblings:** none in a grid; the two-column host matches the skill package route and the install page (§2 Card width does not apply to a detail route, as noted in the skill route).
- **Screenshots:** `/catalog/items/<uuid of a desktop agent>` at 1440 and 1024. No desktop agent in dev data → "required but not capturable"; do not create one.

### S3 — Maintainer controls (People, visibility, delete)

**S3c Maintainer menu.** One `RowActionsMenu` on the title row of the host's facts card, shown when `access='maintain'` or the user `is_superuser` (P2). Items in order: *People…* (opens S3a); skill page only: *Edit details…* (existing `EditSkillPackageDialog`, **minus its Visibility field**, which moves to S3a so visibility has one home); superuser only: *Delist from the catalog* / *List in the catalog* (`PATCH is_listed`; delist keeps its existing `AlertDialog`); separator; *Delete…* `variant="destructive"` (S3b). Hosts: S2 left card; `SkillPackageCard` (its existing menu, extended); `InstallAgentHeaderCard` (new menu on its title row; its `rev N` label switches to `catalogVersionLabel`). Confirms are owned by the card through a `deleteTarget`-style state, not nested in the item (§2 Row actions).

**S3a People dialog — `CatalogPeopleDialog`** (+ `CatalogPeopleForm`, `CatalogGrantRow`, `CatalogPrincipalPicker`), rebuilt from `git show d5bb0a16:frontend/src/components/Admin/TeamLibrary/{LibraryAccessDialog,LibraryAccessForm,LibraryGrantRow,LibraryPrincipalPicker}.tsx` and `frontend/src/utils/libraryAccess.ts` (→ `utils/catalogAccess.ts`). Replaces `SkillPackageAccessCard`, `AddSkillPackageGrantDialog`, `AllSkillPackageGrantsSheet` and `SkillPackageGrantRow` on the skill page (delete them; the card disappears from the right column, which closes one A11 instance).
- **Intent:** "Choose who can find this, who can publish new versions, who looks after it."
- **Layout:** `Dialog` `sm:max-w-lg max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0`; title = item `display_name` (`break-words`), description "Choose who can find this <agent|skill> and who can change it." Body, one section: (1) **General access** `Select` with 3 options — *Anyone signed in* (`public`), *Only the people and roles below* (`users`), *Private* (`private`, helper text "Only people who can share changes or maintain it see it"); when it is not `users` and *Can use* rows exist, one muted line under the list: "Can use rows apply only when access is limited to the people and roles below." (2) the grants list, `max-h-[40vh] overflow-y-auto pr-2`, `ListRowGroup className="mx-0"`, rows = `CatalogGrantRow` (`User` / `Users` glyph tile, title = display name or role label, inline `Select` *Can use / Can share changes / Maintainer* — Maintainer offered on person rows only — and the hover-revealed remove; 2 inline controls, R5); (3) `CatalogPrincipalPicker` — trigger + `Popover` with search, people and roles (no Everyone: that is General access now), already-granted principals excluded (§2 Choosing one entity, §2 The same entity). Footer Cancel / Save.
- **Interaction model:** form (buffered, full replace on Save; Save disabled until dirty and at least one person is Maintainer).
- **States:** loading 3 `Skeleton h-[40px]` rows + disabled Save; error `QueryErrorAlert` "Could not load access" + Retry; empty list "No one is listed yet. Add people or roles below."; pending Save → `LoadingButton` "Saving…", Escape/outside click ignored while pending; refusal (409/422 detail sentence, including "needs at least one maintainer") in the destructive `Alert` above the footer with the edits kept; 404 → toast "This item was already deleted" and close. Grants query `gcTime: 0`, no refetch under the open form (the library dialog's rule).
- **Focus:** first focusable is the General access `Select` (no tooltip) — default auto-focus is fine. After a row remove, focus moves to the next row's remove, else the picker (as the library form).

**S3b Delete confirm — `DeleteCatalogItemDialog`**, rebuilt from `RemoveLibraryAgentDialog` (d5bb0a16). Title "Delete <name> from the catalog?"; description per kind: desktop "People can no longer get it or share changes to it. Copies already on their computers are not deleted."; cloud agent "People can no longer install it. This cannot be undone."; skill "Agents can no longer add it. This cannot be undone." Destructive button "Delete <agent|skill>", pending "Deleting…". On 409 the server's sentence ("{n} people use this. It can be removed once nobody does.") replaces the button in a destructive `Alert`, Cancel becomes Close and takes focus. Success: toast "<name> deleted"; from a detail route navigate to `/catalog`; from S4 stay.

- **Verification (S3a–c):** checklist; straight instances of a rebuilt dialog, a confirm and a menu (§9).

### S4 — Admin catalog (`/admin/catalog`)

- **Intent:** "As the superuser, see every item in the catalog and fix who maintains it, who can get it, or remove it."
- **Layout:** P9, reference `routes/_layout/admin/users.tsx` + `Common/DataTable.tsx`, and the deleted `teamLibraryColumns.tsx` / `TeamLibraryTable.tsx` (d5bb0a16) for the column shapes. Superuser `beforeLoad` guard as `team-library.tsx`. Header "Catalog", subtitle "Every agent and skill in the catalog, who maintains it and who can get it." No primary button (nothing is created on the web). Toolbar above the table on one `justify-between` row: `CatalogKindTabs` (the S1 control, same segments) and a search input (name, id, maintainer). Data `listCatalogItems({scope:'all'})`, client-side search and pagination (`DataTablePagination`, page size 30).
- **Columns (7 + actions):** *Name* (kind glyph `h-3.5 w-3.5` with kind word tooltip, `display_name` truncate; delisted rows muted); *Runs* (kind word: Runs in Cinna / Runs on your computer / Skill); *Maintainers* (≤ 2 names + "+N"); *Who can get it* (General access label; delisted → tone dot `warning` + "Delisted", the accepted DataTable passive-state form); *Version* (`catalogVersionLabel`, empty cell when none); *Installs* ("N people"); *Updated* (`RelativeTime` of `latest.published_at`, empty when none). Row `⋯` (`RowActionsMenu`): *Open* (detail route per kind), *People…*, *Delist from the catalog* / *List in the catalog*, separator, *Delete…*.
- **States:** loading skeleton table rows; error `QueryErrorAlert` "Couldn't load the catalog" + Retry; empty "Nothing is in the catalog yet. Agents and skills appear here when someone shares them." with a link to `/catalog`; search empty "No items match "<q>"."; pending per row: `Loader2` in the `⋯` slot with tooltip + `sr-only` "Deleting…" / "Delisting…" / "Listing…" (§2 Pending on rows); People save pending lives in S3a.
- **AdminMenu:** the *Team Library* entry becomes *Catalog* → `/admin/catalog`, icon `Library`, same position.
- **Screenshots:** `/admin/catalog` at 1440 and 1024 (new route).

### Frontend phase order (all C3, after `make gen-client`)
1. Shared: `catalogVersionLabel`, kind words/glyphs module, `utils/catalogAccess.ts`, `CatalogKindTabs`. Services: `CatalogService.listCatalogItems`.
2. S1 + S1a (`CatalogItemCard`, `CatalogItemGrid`, `CatalogItemFilters`; `/catalog/skills` redirect). Query key `["catalog-items", filters]`; sockets invalidate `["catalog-items"]`.
3. S3a/S3b (`getCatalogItemGrants`, `replaceCatalogItemGrants`, `updateCatalogItem`, `deleteCatalogItem`).
4. S2 + S2a/S2b (`getCatalogItem`, key `["catalog-item", uuid]`).
5. S3c on the three hosts; remove `SkillPackageAccessCard` & co.; `EditSkillPackageDialog` loses Visibility.
6. S4 + `AdminMenu.tsx`.

### Open questions
1. **Naming:** kind segments *Agents in Cinna* / *Agents on your computer* (spec default) versus shorter *Cloud agents* / *Desktop agents*. The tile badges keep U12's *Runs in Cinna* / *Runs on your computer* either way.
2. **Second people editor for cloud agents (out of scope, flagged):** the agent's Bundle tab still carries `BundlePermissionsCard` (email-only *Can use* grants), and the install route redirects a maintainer who has the agent installed to their agent, so they never see S3c there. Retire `BundlePermissionsCard` in favour of a *People…* entry that opens S3a, in a later change?
3. **Contract gap (planner):** `CatalogItem.skills[]` carries `package_id` but not the package uuid, so S2b cannot link a catalog skill to `/catalog/skills/$packageId`. Add `package_uuid` to catalog entries, or accept unlinked rows (spec default).
4. **Contract check (planner):** S3a may save `use` grants while General access is `public`/`private` (P1 says they are kept inert). Confirm `PUT /catalog/items/{uuid}/grants` accepts them for skills, where the old route answered 409 `grants_require_users_visibility`; S3a shows the refusal sentence if not. Save order: `PATCH` visibility first when moving **to** `users`, `PUT` grants first otherwise.

### Lessons for the guideline
- §2 Page toolbar does not say whether an entity **kind** on a merged list is a section or a filter. Decided: a section (segmented control) when the kind changes what each tile's action is; a filter when it only narrows. Write it.
- The catalog browse tile (whole card is the control, one footer action, `auto-rows-fr`, ≤ 7 elements) is used by four components and is not a P-pattern. Name it (P10) with `CatalogItemCard` as its reference after C3.
