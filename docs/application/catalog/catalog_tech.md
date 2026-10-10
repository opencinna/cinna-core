# Catalog — Technical Reference

## File Locations

- Models: `backend/app/models/catalog/catalog_item.py` (`CatalogItem`, `CatalogItemDetail`, `CatalogRevision`, `CatalogInstall`, `CatalogItemsPublic`), `backend/app/models/catalog/catalog_local_install.py` (`CatalogLocalInstall`, `LocalInstallIn`), `backend/app/models/catalog/catalog_access.py`
- Migration: `backend/app/alembic/versions/3c1f7a2d9e40_catalog_local_install.py`
- Routes: `backend/app/api/routes/catalog_items.py` (`/api/v1/catalog`)
- Services: `backend/app/services/catalog/catalog_item_service.py`, `local_install_service.py`, `catalog_events.py`, `catalog_access_service.py`; skill delete in `backend/app/services/skills/skill_catalog_service.py` (`delete_package`); publish hooks in `backend/app/services/bundles/publish_service.py`
- Event types: `backend/app/models/events/event.py` (`CATALOG_UPDATED`, `CATALOG_GRANTED`)
- Frontend: routes `frontend/src/routes/_layout/catalog/index.tsx` (`/catalog`; `catalog/skills/index.tsx` redirects), `frontend/src/routes/_layout/catalog/items/$uuid.tsx` (desktop agent detail), `frontend/src/routes/_layout/admin/catalog.tsx`; components `frontend/src/components/Catalog/` (`CatalogItemCard`, `CatalogItemGrid`, `CatalogItemFilters`, `CatalogKindTabs`, `CatalogItemMenu`, `DeleteCatalogItemDialog`, `DesktopAgent/`, `People/`) and `frontend/src/components/Admin/Catalog/`; events in `frontend/src/hooks/useBundleEvents.ts` and `frontend/src/services/eventService.ts`

## Database

`catalog_local_install`: `id`, `user_id` (FK user, CASCADE), `device_id` (str 128, stored as sent), `item_kind` (`agent`|`skill`), `item_uuid` (no FK, two target tables; rows are removed by the item delete), `revision_number`, `host_manifest_id` (nullable, skills only), `created_at`, `updated_at`. Unique `uq_catalog_local_install (user_id, device_id, item_kind, item_uuid, host_manifest_id)` with nulls not distinct; index on `(item_kind, item_uuid)`.

## API (JWT)

| Route | Notes |
|-------|-------|
| `GET /api/v1/catalog/items` | `kind`, `runs_on`, `scope=all` (superuser, else 403) -> `CatalogItemsPublic` |
| `GET /api/v1/catalog/items/{item_uuid}` | `CatalogItemDetail`; needs `use` |
| `PATCH` / `DELETE /api/v1/catalog/items/{item_uuid}` | maintainer or superuser; delete 409 while others use it |
| `GET` / `PUT /api/v1/catalog/items/{item_uuid}/grants` | full replace |
| `POST /api/v1/catalog/items/{item_uuid}/local-installs` | `LocalInstallIn {device_id, revision_number, host_manifest_id?}` -> `CatalogItem`; 404 without use (skill pinned by a usable desktop agent's latest version passes), 409 `cloud_only`, 422 for missing/extra host id or unknown version |
| `DELETE /api/v1/catalog/items/{item_uuid}/local-installs/{device_id}` | optional `host_manifest_id` query; 204, idempotent, own rows only |

## Skills from a desktop

Routes live in `backend/app/api/routes/skills.py`, logic in `backend/app/services/skills/skill_upload.py` (`SkillUploadService.publish_preview` / `publish`, `read_capped`, `safe_extract`), and the shared write core is `SkillCatalogService._publish_tree` (also used by the cloud-agent publish). See [Agent Skills tech](../../agents/agent_skills/agent_skills_tech.md#desktop-publish).

`SkillCatalogService.get_readable_revision` gates download, content and files: catalog visibility first, else `LocalInstallService.pinned_by_usable_desktop_agent`; otherwise 404 `package_not_found`.

## Service layer

- `LocalInstallService.record` upserts with a single `INSERT ... ON CONFLICT` on `uq_catalog_local_install`; `remove` deletes own rows; `delete_for_item` stages deletion for an item being removed.
- Reads: `people_by_item`, `other_people`, `mine` (builds `CatalogInstall(where='local', ...)`), `cloud_installers`, `pinned_by_usable_desktop_agent` (reads `catalog_skills` pins of the latest revision manifest of every desktop bundle the caller can use).
- `LocalInstallService.update_audience`: grant readers (roles expanded) + cloud installers + local installers who still have access (use, superuser, or the pin rule for skills).
- `CatalogItemService.list_items` batches installs and counts; `install_count` is distinct people over cloud and local rows, publisher excluded. `CatalogItemService.delete` counts other holders (desktop: `DesktopAgentService.other_holders` + local rows; cloud: consumer installs) and raises `CatalogConflictError(people_use_detail(n))`. `SkillCatalogService.delete_package` raises `package_in_use` for other people and `package_linked_by_you` ("Remove it from your agents first.") for the caller's own links.

## Events

`CatalogEvents.revision_published(session, kind, item, revision_number)` is called after a cloud bundle revision commit and after a skill publish; desktop shares use `desktop_agent_revisions.py` and add `head_sha`. Meta `{kind, uuid, id, revision_number[, head_sha]}`; delivered per user to `user_{id}` rooms, failures logged. `CatalogEvents.granted` sends `{kind, uuid}`. Public items: no fan-out to everyone.

## Frontend

`catalog.updated` and `catalog.granted` invalidate catalog queries. Query keys `["catalog-items", filters]` and `["catalog-item", uuid]`; the search params `kind` and `filter` hold the page state. Kind words and glyphs are shared across tile, detail and admin table; one version-label helper renders `v<version>` or `Version <n>`.

## Changelog

- Desktop skill publish and pin-based revision reads documented.
