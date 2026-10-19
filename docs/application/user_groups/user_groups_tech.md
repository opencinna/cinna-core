# User Groups - Technical Details

Migration `7896e6f51c03` (down revision `98e0bd120428`) creates every table and column below, except `agent_api_access_group_grant`, which migration `be8f767e32ed` (down revision `7896e6f51c03`) adds. That table is unique on `(producer_agent_id, group_id)`, has `scopes` JSON, `created_by` nullable SET NULL (DEFERRABLE INITIALLY DEFERRED), and CASCADE FKs to `agent` and `user_group`; model `backend/app/models/agent_api/agent_api_access_group_grant.py`, kept apart from `agent_api_access_grant` so an unaware read site fails closed.

## File Locations

- Models: `backend/app/models/users/user_group.py` (`UserGroup`, `UserGroupMember`, API schemas), `backend/app/models/credentials/credential_group_share.py`, `backend/app/models/credentials/ai_credential_group_share.py`, `backend/app/models/artifacts/artifact_grant.py` (`ArtifactGroupGrant`), `backend/app/models/knowledge/knowledge.py` (`AIKnowledgeGitRepoGroupShare`), `backend/app/models/catalog/catalog_access.py` (`CatalogPrincipalGroup`)
- Services: `backend/app/services/users/user_group_service.py`, `backend/app/services/users/user_group_access.py`, `backend/app/services/users/user_group_shares_service.py`
- Routes: `backend/app/api/routes/admin_user_groups.py`, `backend/app/api/routes/user_groups.py`

## Database

| Table | Notes |
|---|---|
| `user_group` | `id`, `name` (100; unique index on `lower(name)`), `description` (500), `created_by_user_id` (FK user, SET NULL, deferrable), timestamps |
| `user_group_member` | `group_id` (CASCADE), `user_id` (CASCADE), `created_at`; unique `(group_id, user_id)`; no `added_by` |
| `bundle_access_grant`, `skill_package_access_grant` | new nullable `group_id` FK; `principal_kind` gains `group` |
| `credential_group_share` | one credential x one group, with `shared_by`; separate from `credential_shares` |
| `ai_credential_group_share` | same shape for `ai_credential` |
| `artifact_group_grant` | `artifact_id`, `group_id`, `level` (`view`), `granted_by`; unique `(artifact_id, group_id)` |
| `ai_knowledge_git_repo_group_share` | `git_repo_id`, `group_id`; knowledge-source query share |

Every SET NULL foreign key on the new tables is `DEFERRABLE INITIALLY DEFERRED` (double-cascade on user delete).

## API

All admin routes require a superuser (`get_current_active_superuser`).

| Route | Purpose |
|---|---|
| `GET /api/v1/user-groups/search?q=&limit=` | Any authenticated user; limit clamped 1-25; returns `{id, name, member_count}` only |
| `GET`, `POST /api/v1/admin/user-groups/` | List (with `member_count`, `share_count`) / create |
| `GET`, `PATCH`, `DELETE /api/v1/admin/user-groups/{group_id}` | Detail / rename (empty PATCH no-op) / delete with coverage release |
| `GET`, `POST /api/v1/admin/user-groups/{group_id}/members` | List / add (`user_ids`, 1-100, active users, idempotent) |
| `DELETE /api/v1/admin/user-groups/{group_id}/members/{user_id}` | Remove, then release lost access |
| `GET /api/v1/admin/user-groups/{group_id}/delete-impact` | `UserGroupImpact` preview |
| `GET /api/v1/admin/user-groups/{group_id}/members/{user_id}/removal-impact` | Preview for one member (route in `admin_user_groups.py`) |
| `GET /api/v1/admin/user-groups/{group_id}/shares` | Everything shared with the group (`UserGroupSharesPublic`) |
| `GET .../shares/{resource_type}/{resource_id}/revoke-impact` | Preview for one share |
| `DELETE /api/v1/admin/user-groups/{group_id}/shares/{resource_type}/{resource_id}` | Superuser revoke, overriding the owner |

`resource_type`: `agent_bundle`, `desktop_agent`, `skill_package`, `credential`, `ai_credential`, `artifact`, `knowledge_source`, `agent_api_scopes` (`resource_id` = producer agent id, `level` = comma-joined scope names or `"no scopes"`; revoke deletes the group's scope row via `UserGroupSharesService._revoke_agent_api_scopes`; impact previews only count it, no loss computation; group delete relies on FK CASCADE). Errors map: not found 404, name or state conflict 409, validation (unknown users) 422.

Share endpoints in other features (owner-facing):

| Route | Feature |
|---|---|
| `PUT /api/v1/catalog/items/{item_uuid}/grants` with `principal: {kind: "group", group_id}` | catalog |
| `GET`, `POST /api/v1/agents/{agent_id}/agent-api/group-grants`, `PUT`, `DELETE .../group-grants/{grant_id}` (owner-gated; POST body `{group_id, scopes}`; 409 duplicate) | Agent REST API (`routes/agent_api.py`, `AgentApiGrantService`) |
| `POST /api/v1/agents/{agent_id}/bundle-permissions/group-grants` (`{group_id}`, always grants `use`; 201; 404 unknown group, 409 duplicate), `DELETE .../group-grants/{grant_id}` (204; 404 if not a group grant of this bundle) | agent bundles (`routes/installs.py`; publisher install + `BundleService.get_for_publisher`) |
| `POST`, `GET /api/v1/credentials/{id}/group-shares`, `DELETE .../group-shares/{group_id}` | credentials (`credential_shares.py`) |
| `POST`, `GET /api/v1/ai-credentials/{id}/group-shares`, `DELETE .../group-shares/{group_id}`, `GET /api/v1/ai-credentials/shared-with-me` | AI credentials |
| `PUT /api/v1/artifacts/{id}/grants` (people or groups; role principal is 422) | artifacts |
| `GET`, `POST /api/v1/knowledge-sources/{id}/shared-groups`, `DELETE .../shared-groups/{group_id}` | knowledge sources (superuser) |

## Services

- `UserGroupService` - get / list / search / create / update / `add_members` (ON CONFLICT DO NOTHING) / `remove_member` (snapshot coverage, delete, commit, release); typed errors `UserGroupNotFoundError`, `UserGroupConflictError`, `UserGroupNameConflictError`, `UserGroupValidationError`, `UserGroupUnknownUsersError`.
- `UserGroupAccess` - the one seam: `member_group_ids_subquery(user_id)`, `active_member_ids`, `member_counts` (active only), `member_ids`, `coverage(session, group_id, user_ids=None)` returning a frozen `GroupCoverage` (`credential_ids`, `ai_credential_ids`, `user_ids`), and `release(session, coverage)` which hands pairs to the credential and AI credential release hooks.
- `UserGroupSharesService` - `list_shares`, `revoke`, `impact`, `member_removal_impact`, `share_revoke_impact`, `delete_group`. Delete precomputes coverage before the delete, so release touches only pairs the group covered.
- `CatalogAccessService` - group principals folded into `level_from`; group grants refused above `use`/`edit` (reuses `PEOPLE_ONLY_MAINTAIN_DETAIL`). `add_group_grant` / `remove_group_grant` are the single-group writes behind the bundle Permissions card; they apply the same `_check_principal_rule` as the catalog PUT, log the same `CATALOG_GRANTS_CHANGED` event, and `add_group_grant` sends `catalog.granted` to newly gained members (separate from the replace-all PUT).
- `CredentialShareService` - `share_with_group`, `list_group_shares`, `revoke_group_share`, `group_shared_credential_ids_subquery`, `via_groups`, `users_without_access` (owner / direct / group), `get_group_share_count_for_credential` (`group_share_count` on owned `CredentialPublic`). `CredentialsService.unlink_credential_from_revoked_recipients` filters recipients through `users_without_access` first (D8).
- `ai_credentials_service` - `share_with_group` (refuses admin-managed via `is_shareable`), `list_group_shares`, `revoke_group_share`, `list_shared_with_me`, `users_without_access`; on loss NULLs both env pin columns and restarts running environments in background tasks (strong task references kept).
- `ArtifactService` / `artifact_access` - group grants OR-ed into list (`shared_with_me`) and access caps; `replace_grants` takes people or groups.
- `knowledge_access_service` / `knowledge_source_service` - shared source visible through a user share or a group share; `list_shared_groups`, `add_shared_group`, `remove_shared_group`.
- `desktop_credential_service` - a credential held only through a group maps to a `None` source (direct share) for local copy.

## Frontend

- Routes: `frontend/src/routes/_layout/admin/user-groups.tsx`, `frontend/src/routes/_layout/admin/user-group/$groupId.tsx`.
- Admin components: `frontend/src/components/Admin/UserGroups/` (`UserGroupsTable`, `UserGroupMembersTab`, `UserGroupSharesTab`, `UserGroupImpactDialog` shared by `DeleteUserGroupDialog`, `RemoveUserGroupMemberDialog`, `RevokeUserGroupShareDialog`, `AddUserGroupMembersDialog`, `UserGroupFormDialog`, `userGroupQueries.ts`).
- Pickers: `frontend/src/components/Common/UserAllowlistPicker.tsx` (`groups`, `people`), `frontend/src/components/Common/useUserGroupSearch.ts`, `frontend/src/components/Catalog/People/CatalogPrincipalPicker.tsx` (`allowGroups`), `frontend/src/utils/principalKinds.ts`.
- Bundle Permissions card: `frontend/src/components/Agents/BundlePermissions/BundlePermissionGroups.tsx` (Groups section), `BundlePermissionsCard.tsx`, `BundlePermissionsAddUserModal.tsx` (person-or-group picker, `UserAllowlistPicker` groups mode). Tests: `backend/tests/api/agents/bundles/agents_bundle_permissions_groups_test.py`.
- Credentials: `frontend/src/components/Credentials/CredentialSharing.tsx`, `frontend/src/components/Credentials/UnshareCredentialGroupDialog.tsx`.
- AI credentials: `frontend/src/components/UserSettings/AICredentials/AICredentialGroupShareDialog.tsx`, `AICredentialRow.tsx`; `frontend/src/components/Environments/useSharedAICredentials.ts` feeds the "Shared with you" keys in environment config.
- Knowledge: `frontend/src/components/KnowledgeSources/KnowledgeSourceAccessCard.tsx`.

## Security

Group search never exposes membership. Member-level data (emails) is superuser-only. Every mutation is audited via `add_audit_event` / security events.
