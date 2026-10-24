# Addons tests

Covers the unified addons core (`docs/plans/unified_addons_plan.md`, K3 + K4):

- `addons_directory_test.py` — `GET /api/v1/addons/directory`: the union of
  catalog skills, marketplace skills/plugins and MCP directory entries,
  visibility per caller, ranking, filters, ETag / 304, policy.
- `mcp_directory_test.py` — `/api/v1/mcp-directory/*`: superuser-only CRUD,
  validation, ban / unban, and the opencinna import.
- `marketplace_access_test.py` — K5: marketplace access mode, per-entry
  allow / ban, visibility across discover, batch, archives and the directory,
  mode-switch counts, bulk allow. Related: `tests/api/server_config/addons_policy_test.py`
  (the four policy flags) and `tests/migrations/marketplace_access_test.py`.

## Conventions

- API-only, as `tests/README.md` requires. Catalog skills are published from a
  stubbed-environment agent (`tests/api/catalog/catalog_helpers.py`);
  marketplaces are synced against fixture repos (`tests/utils/llm_plugin.py`,
  `tests/utils/marketplace_skills.py`); git is never contacted.
- The opencinna index fetch is stubbed (`import_mcp_index` in
  `tests/utils/addons.py` patches `McpDirectoryService.fetch_index`); the
  network is never touched.
- `conftest.py` has the agent-environment stubs plus an autouse fixture that
  clears the directory's per-user index cache around each test. Tests never
  rely on its 60 s TTL: every write bumps `addons_directory_version`, which is
  part of the cache key, so a write changes the ETag at once.
