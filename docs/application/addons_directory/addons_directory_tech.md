# Addons Directory — Technical Reference

## File Locations

- Models: `backend/app/models/addons/directory.py` (`DirectoryEntry`, `DirectoryRef`, `DirectoryPublisher`, `DirectoryOrigin`, `DirectoryRequires`, `McpConnectDescriptor`, `AddonsPolicy`, `AddonsDirectoryPublic`), `backend/app/models/mcp/mcp_directory_entry.py` (`McpDirectoryEntry` table, `Create` / `Update` / `Public` / `EntriesPublic`, `McpDirectoryAccessUpdate`, `McpDirectoryImportResult`)
- Migration: `backend/app/alembic/versions/ab48f9755987_addons_directory_and_mcp_directory.py` (table `mcp_directory_entry`, column `server_config.addons_directory_version`)
- Routes: `backend/app/api/routes/addons.py`, `backend/app/api/routes/mcp_directory.py`
- Services: `backend/app/services/addons/addon_directory_service.py`, `backend/app/services/addons/directory_version.py`, `backend/app/services/addons/summary.py`, `backend/app/services/mcp/mcp_directory_service.py`
- Config: `MCP_DIRECTORY_INDEX_URL` in `backend/app/core/config.py` (default `https://opencinna.io/.well-known/mcp_registry/index.json`)
- Frontend: `frontend/src/routes/_layout/admin/mcp-directory.tsx`, `frontend/src/components/Admin/McpDirectory/`, entry in `frontend/src/components/Sidebar/AdminMenu.tsx`
- Tests: `backend/tests/api/addons/` (`addons_directory_test.py`, `mcp_directory_test.py`, `README.md`), helpers `backend/tests/utils/addons.py`

## API

| Method and path | Auth | Notes |
|---|---|---|
| `GET /api/v1/addons/directory` | `CurrentUser` | Query `search`, `kind` (`skill`/`plugin`/`mcp`), `category`, `source`, `runs_on=desktop`, `skip`, `limit` (1..5000). Sets `ETag`; `If-None-Match` match returns 304 |
| `GET /api/v1/mcp-directory/entries` | superuser | All entries including banned, ordered by title |
| `POST /api/v1/mcp-directory/entries` | superuser | Create a manual entry |
| `PUT /api/v1/mcp-directory/entries/{entry_id}` | superuser | Update; changing transport does not require nulling the other field |
| `DELETE /api/v1/mcp-directory/entries/{entry_id}` | superuser | Imported entries return on the next import |
| `PUT /api/v1/mcp-directory/entries/{entry_id}/access` | superuser | Body `access` (`default`/`banned`), optional `reason` (kept only when banned); records who and when |
| `POST /api/v1/mcp-directory/import` | superuser | Returns `McpDirectoryImportResult` |

MCP directory errors are `detail = {code, message}`: 404 `entry_not_found`, 409 `name_taken`, 422 `invalid_entry` / `index_version_unsupported`, 502 `index_fetch_failed` / `index_too_large` / `index_invalid`.

## Service layer

`AddonDirectoryService` (`addon_directory_service.py`):
- `directory(session, user, DirectoryQuery)`: index, filter, rank, page, policy, ETag.
- `build_index`: `_catalog_entries` (`SkillCatalogService.list_catalog`, drops unlisted unless `can_manage`, digest `sha256:<content_hash>`, archive path `/skills/packages/{id}/revisions/{n}/download`), `_marketplace_entries` (join with `LLMPluginMarketplace` under `LLMPluginService._discoverable_clause`, archive path `/llm-plugins/plugins/{id}/archive?sha256=`), MCP rows from `McpDirectoryService.visible_entries`.
- `index_for`: cache `{(user_id, version): (built_at, entries)}`, TTL 60 s (`CACHE_TTL_SECONDS`), 256 entries LRU; `clear_cache()` for tests.
- `apply_filters`, `match_tier`, `rank`, `policy_for` (returns defaults, all true), `etag_of` (`"` + first 32 hex of sha256 of the sorted-key JSON of data, count, policy + `"`).
- Catalog `runs_code` is cached per immutable revision id; a missing snapshot on disk is not cached.

`AddonsDirectoryVersion.bump(session)` increments the column inside the caller's transaction without committing (a bare `UPDATE`); `current(session)` reads it. Callers: `LLMPluginService` (marketplace create/update/delete, sync, a status path), `plugin_snapshot_worker` (batches that changed a row), `SkillCatalogService` (publish, update, delist, grants), and every write in `McpDirectoryService`.

`McpDirectoryService`: CRUD with `_validate` (transport vs url/package, HTTPS, env names), `set_access`, `fetch_index` (`httpx`, `follow_redirects=False`, `INDEX_TIMEOUT_SECONDS`, `INDEX_MAX_BYTES` = 1 MB, `INDEX_DEADLINE_SECONDS` = 20), `apply_index` (new rows inside a savepoint so a racing manual create of the same name only skips that row; bumps the version only if something changed), `_map_index_server`, `to_directory_entry`. Import-owned columns are listed in `_INDEX_FIELDS`.

## Models

`McpDirectoryEntry` (`mcp_directory_entry`): `name` (unique slug, becomes the entry `name` and the id seen by the index), `title`, `description`, `icon_url`, `categories` (JSON), `transport` (`stdio|http|sse`), `url`, `package` (JSON: registry, name, version, args), `auth` (`none|oauth|bearer|env`), `env` (JSON of name, secret, description), `homepage`, `publisher_name`, `verified`, `source` (`manual|opencinna`), `source_ref`, `access` (`default|banned`), `access_reason`, `access_changed_by_id` (FK user, SET NULL), `access_changed_at`, `created_by_id`, timestamps.

## Frontend

The admin MCP Directory page lists entries, offers an add wizard, edit, ban/unban, delete and an import dialog; it uses the generated client services for the `mcp-directory` tag.

The add wizard and the edit Sheet share one controlled draft model and validator (`components/Admin/McpDirectory/mcpEntryDraft.ts`) instead of react-hook-form: the form's shape changes with the transport and the env list is a repeating group, both surfaces render the same field components, and the edit Sheet's dirty tracking and partial update (only changed fields are sent, never `null` on a required one) are computed by comparing wire bodies. The validator mirrors the service's rules and caps so most refusals appear beside the field. The ban dialog (`components/Admin/Addons/BanEntryDialog.tsx`) is generic so the marketplace entries list can reuse it.
