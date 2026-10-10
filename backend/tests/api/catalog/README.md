# Catalog tests

Covers the one catalog of agents and skills (`docs/plans/catalog_unification_plan.md`,
phases C1–C5): the access model shared by agent bundles and skill packages, and
`/api/v1/catalog/items/...`.

## API-only

Everything goes through HTTP, as `tests/README.md` requires. Items are published
from real (stubbed-environment) agents: bundles through `POST /agents/{id}/publish`,
skill packages from a skill folder seeded in the agent's host-side workspace
(`tests/utils/skill_catalog.write_skill`). Grants are set with
`PUT /catalog/items/{uuid}/grants`; who sees what is read back from
`GET /catalog/`, `GET /bundles/{uuid}`, `GET /skills/catalog` and
`GET /skills/packages/{uuid}`.

The migration that converted the old grant rows is tested separately in
`tests/migrations/catalog_access_grants_test.py` (scratch DB, raw SQL seeds).

## Fixtures (`conftest.py`)

- Agent-environment stubs, as in `tests/api/agents/` and `tests/api/library/`.
- `skill_storage` (autouse): `SKILL_STORAGE_DIR` → tmp tree.
- `socket_stub`: captures Socket.IO emits; `catalog.granted` lands in the
  `user_<id>` room of each newly reached user.

`catalog_helpers.py` holds the domain's HTTP helpers (`make_person`,
`make_non_admin_superuser`, `publish_agent_bundle`, `publish_skill_package`,
`put_item_grants`, `user_grant` / `role_grant`, ...). Move them to
`tests/utils/catalog.py` once another domain needs them.

## Real git, honestly (from C2)

Desktop agents (C2) are bundles whose content store is a git repo. Those tests
run a real `git` subprocess against the app through
`tests/utils/git_http_bridge.py`: the bridge forwards each request (raw/chunked
body, headers) to the `TestClient`, git env comes from `GitClient`
(`tests/utils/catalog.py`: token via `http.extraHeader`, no prompt, no
credential helper, no global config). The git route's authorize step and
`DesktopAgentRevisions._record_push` open their own `Session(engine)` by
design; `conftest.py` patches both to a non-closing proxy of the test session
(`patch_git_path_sessions`, autouse) and points `GIT_STORAGE_DIR` at a tmp dir
(`git_storage`). A pushed tree must carry the agent's bound manifest id:
`write_agent_tree(path, manifest_id=item["desktop"]["manifest_id"])`.
`bind_cloud_install_seam` inserts an `AgentGitSource` row directly (Rule-1
exemption: cloud checkouts of core repos now answer `desktop_only`).

## Files (one scenario file per phase topic)

C1 — one access model:
- `catalog_access_test.py` — visibility × grant level × role matrix for bundles
  and skills, role grants (`admin` role ⇢ superuser), publish needs `edit`.
- `catalog_grants_test.py` — `GET/PUT /catalog/items/{uuid}/grants` gates,
  validation, full replace, `catalog.granted`, audit, and the old
  `/bundles/*/grants` and `/skills/packages/*/grants` routes.

C2 — desktop agents are bundles:
- `catalog_desktop_git_test.py` — real git round trip (v0+v2, Version N per
  push, notes = commit messages), non-ff/delete, auth + token kinds, access
  matrix over the wire, grants over real git, gzip body, `catalog.updated` +
  audit (moved from `tests/api/library/`).
- `catalog_desktop_push_policy_test.py` — one push per refusal sentence,
  including a changed manifest id.
- `catalog_desktop_agents_test.py` — `POST /catalog/desktop-agents`: create,
  validation, slug / manifest-id conflicts, minted `bundle_id`, grants.
- `catalog_desktop_only_guards_test.py` — every cloud path answers 409
  `desktop_only` for a desktop agent; old lists show cloud bundles only.
- `catalog_items_test.py` — `GET/PATCH/DELETE /catalog/items[/{uuid}]`:
  listing rules (P3/P4, `scope=all`), detail with revisions, management gates,
  delete guards ("1 person uses this…" / "N people use this…").
- `catalog_desktop_revisions_test.py` — each accepted share becomes a revision
  (notes, manifest projection, credential specs, skills summary).
- `catalog_desktop_user_delete_test.py` — deleting a desktop agent's creator
  (self or by a superuser) leaves the agent ownerless and managed by superusers.

C3 — unified listing, local installs, events:
- `catalog_local_installs_test.py` — `POST/DELETE /catalog/items/{uuid}/local-installs`:
  gate, validation, upsert (one row, version moves), per-device / per-host
  rows, idempotent own-rows-only delete, `cloud_only` 409, superuser gate,
  U7 (a skill reached through a usable desktop agent's latest pin), and the
  delete guard counting other people's local installs (agent and skill).
- `catalog_listing_matrix_test.py` — `GET /catalog/items` visibility × grants
  × roles across cloud bundle, desktop agent and skill; P4 with a role
  `edit` grant; `install_count` / `my_installs` across cloud + local
  (publisher excluded; `/skills/catalog` count agrees).
- `catalog_events_test.py` — `catalog.updated` audience per publish kind
  (cloud bundle, skill, desktop push): grant readers + cloud installers;
  local installers only while they still have access; no public fan-out.

Unit (no database): `tests/unit/test_push_policy_bound_manifest_id.py` (the
policy's bound manifest id), `tests/unit/test_desktop_bundle_id.py` (minting).

## Behaviour pinned here that is easy to get wrong

- A `use` grant counts only under `visibility='users'` while listed; `edit` and
  `maintain` count at any visibility and listing state.
- A delisted public item is never offered in a listing, superusers included;
  a superuser can still open it directly.
- The old per-kind grant lists show user grants without the publisher's own
  `maintain` row; co-maintainers appear with their `access`. Re-granting never
  downgrades.
- `catalog.updated` follows effective grants: a `use` grant is inert under
  `public`, so its holder stops hearing updates once an item goes public
  (public reach never fans out); `edit`/`maintain` holders and installers
  keep hearing them.
