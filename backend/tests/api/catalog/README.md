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
`tests/utils/git_http_bridge.py`, exactly as `tests/api/library/README.md`
describes: the bridge forwards each request to the `TestClient`, git env comes
from `GitClient` (token via `http.extraHeader`, no prompt, no credential
helper, no global config), and the git path's own `Session(engine)` call sites
are patched to a non-closing proxy of the test session. Copy that seam into
this `conftest.py` when the first git test lands here.

## Files (one scenario file per phase topic)

C1 — one access model:
- `catalog_access_test.py` — visibility × grant level × role matrix for bundles
  and skills, role grants (`admin` role ⇢ superuser), publish needs `edit`.
- `catalog_grants_test.py` — `GET/PUT /catalog/items/{uuid}/grants` gates,
  validation, full replace, `catalog.granted`, audit, and the old
  `/bundles/*/grants` and `/skills/packages/*/grants` routes.

## Behaviour pinned here that is easy to get wrong

- A `use` grant counts only under `visibility='users'` while listed; `edit` and
  `maintain` count at any visibility and listing state.
- A delisted public item is never offered in a listing, superusers included;
  a superuser can still open it directly.
- The old per-kind grant lists show user grants without the publisher's own
  `maintain` row; co-maintainers appear with their `access`. Re-granting never
  downgrades.
