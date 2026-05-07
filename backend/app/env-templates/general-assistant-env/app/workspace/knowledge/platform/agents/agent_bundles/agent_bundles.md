# Agent Bundles & Installs

## Purpose

Replace the clone-based sharing model with a desktop-app-style **bundle / install** system. An agent developer packages their agent as a versioned, publisher-owned **bundle** identified by a stable reverse-DNS string. Other users **install** the bundle, each receiving their own running copy. A separate per-user **App Data** area survives uninstall and reattaches on reinstall, so publishers can ship updates without ever overwriting user state.

## Core Concepts

- **Bundle** (`AgentBundle`) — canonical metadata record owned by a publisher. Uniquely identified by a reverse-DNS `bundle_id` (e.g. `io.opencinna.cinna.a1b2c3d4`). One bundle per published agent on this instance
- **Bundle Revision** (`AgentBundleRevision`) — immutable snapshot of a bundle's content taken at publish time. Contains workspace folder copies, prompts, SDK selection, `required_credential_specs`, and an optional human-friendly `version` label entered by the publisher. Revisions are append-only; the latest is pointed to by `AgentBundle.latest_revision_id`
- **Install** — every `Agent` row is an install in the new model. The publisher's working copy (`is_publisher_install=True`) and every foreign user's copy are all `Agent` rows, distinguished by `is_publisher_install` and `bundle_uuid`
- **Publisher Install** — the `Agent` row owned by the bundle publisher. This is the source of truth for the next publish snapshot. The publisher develops the agent here as normal; clicking "Publish" snapshots its current workspace state
- **App Data** — a per-user, per-bundle persistent volume mounted at `/app/workspace/app-data` inside the container. Keyed by `(user_id, bundle_id)`. Survives uninstall; reattaches on reinstall. See [Agent App Data](../agent_app_data/agent_app_data.md)
- **Bundle ID** — reverse-DNS string, auto-generated on agent creation. Format: `<reversed-host>.<8-hex-chars-of-agent-uuid>` (e.g. `io.opencinna.cinna.a1b2c3d4`). The publisher can override the auto-generated value inside the **first** publish dialog (it's the moment the bundle is defined); after the first revision is recorded, the bundle ID is locked
- **Version label** — optional, user-supplied string captured per revision (e.g. `1.0`, `1.1`, `2.0`). Independent from the internal monotonic `revision_number` — `revision_number` continues to drive snapshot paths and ordering. The publish dialog defaults the field to `1.0` on the first publish and suggests a minor bump from the previous revision afterwards
- **Visibility** — controls catalog access: `private` (publisher only), `users` (explicit allowlist via `BundleAccessGrant`), `public` (all users on this instance)
- **Update Mode** — per-install setting controlling how revisions are applied: `manual` (user applies explicitly) or `automatic` (applied by the suspension scheduler while the environment is idle)

## User Stories / Flows

### Publishing a Bundle (agent-developer)

1. Developer opens an agent they own and navigates to the **Bundle tab**
2. Clicks "Publish" — the `PublishDialog` opens with three fields:
   - **Bundle ID** — only on the first publish; prefilled with the auto-generated value, editable. Locked once the first revision exists
   - **Version** — required; defaults to `1.0` on the first publish, otherwise suggests a minor bump from the previous revision (e.g. `1.0` → `1.1`). Manually editable for major releases
   - **Release notes** (optional)
3. On submit: workspace folders (`scripts/`, `docs/`, `knowledge/`, `files/`, `workspace_requirements.txt`, `workspace_system_packages.txt`) are copied to `<BUNDLE_STORAGE_DIR>/<bundle_id>/<revision>/`; a new `AgentBundleRevision` row is created with the user-entered `version`; `BUNDLE_PUBLISHED` WebSocket event fires
4. On first publish the `AgentBundle` row is also created; the `bundle_id` value submitted in the publish form is applied to the agent before the snapshot. Subsequent publishes append a new revision
5. All existing foreign installs receive `INSTALL_UPDATE_AVAILABLE` events (manual mode) or are marked for next-idle-cycle update (automatic mode)
6. The revision appears in the revisions list on the Bundle tab with a "current" badge — labeled `v<version>` when present, falling back to `rev <revision_number>` for legacy revisions

### Installing a Bundle (any user)

1. User opens the **Catalog** page; sees all public bundles and any bundles explicitly granted to them
2. Clicks **Install** on a catalog card
3. The **Install Wizard** opens (4 steps):
   - Step 1: Overview (name, description, publisher handle, required credentials)
   - Step 2: Credentials — for each `required_credential_spec` pick an existing user credential or accept a placeholder
   - Step 3: AI Credentials — select conversation and building mode LLM credentials
   - Step 4: Confirm and install
4. On submit: a new `Agent` row is created seeded from the latest revision; an environment is provisioned and started; the App Data volume for `(user, bundle_id)` is created (or reattached if previously orphaned)
5. Loading state with environment progress display; on activation redirect to the install detail page

### Applying an Update (install owner)

1. WebSocket event `INSTALL_UPDATE_AVAILABLE` arrives; **UpdateAvailableBanner** appears on the install detail page
2. User reviews release notes in the banner or modal
3. Clicks "Apply update":
   - If automatic mode: the suspension scheduler applies the update the next time the environment goes idle
   - If manual mode: user confirms; environment stops, bundle folders replaced from the new revision snapshot, environment restarts; app-data and credentials are preserved
4. `INSTALL_UPDATE_APPLIED` event fires; banner clears; `installed_revision_id` advances

### Uninstalling (install owner)

1. User clicks "Uninstall" on the install detail page
2. Confirmation dialog shown ("stop and uninstall?")
3. Environment stopped and removed (bundle workspace volume removed; app-data volume preserved)
4. `Agent` row deleted; `AppDataVolume.is_orphaned` set to `true`
5. App data remains visible in Settings → App Data as an orphaned entry

### Reinstalling the Same Bundle

The install wizard is re-entered; when `InstallService` calls `AppDataService.get_or_create_volume`, it finds the orphaned row, clears `is_orphaned`, and reattaches it — preserving all previous user data.

### Managing Visibility and Grants (publisher)

1. Publisher opens Bundle tab → sets visibility to `users`
2. Opens Grants section; enters email address of a user to grant
3. The `BundleAccessGrant` row is created; the target user now sees the bundle in the catalog
4. Publisher can revoke individual grants; revoking all grants leaves the bundle in `users` visibility but invisible to everyone except the publisher

### Deleting a Revision (publisher)

1. Publisher opens the Bundle tab; each row in the Revisions list shows a delete button. The button is disabled and shows a tooltip ("Cannot delete — N installs reference it") whenever any non-publisher install is on that revision; the publisher's own working install does not block deletion
2. Clicking it opens a confirmation dialog explaining what happens (snapshot tree removed, `bundle.latest_revision_id` rewired to the previous revision when needed, publisher install detached)
3. On confirm: the `AgentBundleRevision` row is deleted, the on-disk snapshot tree at `<BUNDLE_STORAGE_DIR>/<bundle_id>/<revision_number>/` is removed best-effort, any publisher install pointing at the revision has its `installed_revision_id` cleared, and — if the deleted revision was the bundle's current revision — `latest_revision_id` is moved to the most-recent remaining revision (or `NULL` when none remain)
4. **When the last revision is removed, the bundle row is auto-deleted** — grants are cascaded, and the publisher install's `bundle_uuid` is cleared by the FK `ON DELETE SET NULL`. The agent reverts to "unpublished" (Bundle ID becomes editable again, catalog settings are gone, the next publish creates a fresh `AgentBundle`)
5. Foreign installs are never affected because the API refuses the call while any are still on the revision

### Deleting a Bundle

The API rejects deletion when any foreign install (non-publisher) references the bundle (409). The publisher must have those users uninstall first. On successful deletion, all `AgentBundleRevision` rows and `BundleAccessGrant` rows are cascade-deleted; the publisher's `Agent` row has `bundle_uuid` set to `NULL` by the FK `ON DELETE SET NULL`.

## Business Rules

- **Bundle ID is set inside the first publish dialog and immutable afterwards** — there is no separate edit modal; the publisher enters the final ID alongside the version label and release notes when publishing the very first revision. Changing it post-publish would silently orphan installed app-data volumes on all foreign installs (app-data keyed on the string `bundle_id`, not the `AgentBundle` UUID), so the API rejects post-publish overrides with 409
- **Publisher install cannot be uninstalled** — use the bundle management UI (delete agent + delete bundle) instead
- **One publisher install per bundle** — enforced by partial unique index on `bundle_uuid WHERE is_publisher_install = true`
- **One install per user per bundle** — `install_bundle` is idempotent; returns the existing install if already present
- **Publish requires agent-developer role** and ownership of the install
- **Workspace separation on update** — `replace_bundle_content` overwrites only bundle-owned folders (`scripts/`, `docs/`, `knowledge/`, `files/`, `workspace_requirements.txt`, `workspace_system_packages.txt`); `app-data/` and `credentials/` are never touched
- **Empty workspace publish is allowed** — revision is created with empty bundle folders; UI shows a warning
- **Publish failures leave a `.tmp` directory** — the bundle's `latest_revision_id` is unchanged on failure; the partial snapshot at `<snapshot_path>.tmp` is available for debugging
- **Per-bundle publish lock** — an in-process `asyncio.Lock` serialises concurrent publishes for the same bundle ID; the DB unique constraint on `(bundle_id, revision_number)` catches the cross-process race
- **Credential specs carry metadata only** — `required_credential_specs` in a revision contains `{name, type, allow_sharing}` pairs from the publisher's linked credentials at publish time. No secret values are ever stored in a revision
- **Revision delete blocked by foreign installs** — `DELETE /bundles/{uuid}/revisions/{revision_id}` rejects (409) when any non-publisher install still has `installed_revision_id` pointing at the revision. Only the publisher's own working install is auto-detached. Deleting the bundle's current revision rewires `latest_revision_id` to the previous remaining revision (or `NULL`); the on-disk snapshot directory is removed best-effort and a leftover failure is logged but does not block the DB delete
- **Last revision auto-unpublishes the bundle** — when revision delete leaves a bundle with zero revisions, the empty `AgentBundle` row is deleted in the same transaction. Cascades remove `BundleAccessGrant` rows; the publisher install's `bundle_uuid` is cleared via FK. This re-enables the "Bundle ID is immutable after first publish" gate (it becomes editable again because the publisher install is no longer linked to a bundle row)

## Architecture Overview

```
Publisher's Install (Agent row)
  is_publisher_install = True
  bundle_id = "io.opencinna.myagent.a1b2c3d4"
  bundle_uuid → AgentBundle.id
         │
         └── POST /agents/{id}/publish
                   │
                   ├── snapshots workspace folders
                   ├── writes <BUNDLE_STORAGE_DIR>/<bundle_id>/<rev>/
                   ├── inserts AgentBundleRevision
                   ├── updates AgentBundle.latest_revision_id
                   └── notifies foreign installs
                                  │
                                  ▼
                        Other User's Install (Agent row)
                          is_publisher_install = False
                          bundle_uuid → same AgentBundle.id
                          installed_revision_id → AgentBundleRevision.id
                          pending_update = True (if behind)
                                  │
                                  └── app-data/ (persistent, per-user×bundle)
```

## Integration Points

| Feature | Relationship |
|---------|-------------|
| [Agent App Data](../agent_app_data/agent_app_data.md) | Every install gets exactly one `AppDataVolume` keyed by `(user_id, bundle_id)`. The volume is created/reattached by `InstallService` and mounted at `/app/workspace/app-data` inside the container |
| [Agent Environments](../agent_environments/agent_environments.md) | `EnvironmentLifecycleManager` mounts the app-data volume; `replace_bundle_content()` swaps bundle folders during apply-update |
| [Agent Environment Data Management](../agent_environment_data_management/agent_environment_data_management.md) | Bundle-owned folders are replaced on update; app-data and credentials are preserved |
| [Agent Prompts](../agent_prompts/agent_prompts.md) | Prompts are snapshotted into the revision at publish time; `apply_update` syncs them back onto the install's `Agent` row |
| [Agent Credentials](../agent_credentials/agent_credentials.md) | `required_credential_specs` describes what credentials the bundle needs; install wizard creates placeholders or links existing credentials |
| [User Roles](../../application/user_roles/user_roles.md) | `publish` and `edit-bundle-id` require `agent-developer` or `admin`; catalog browse and install are available to all authenticated users |
| [Email Integration](../../application/email_integration/email_sessions.md) | `install_bundle_for_email` replaces the legacy auto-share flow; first inbound email from a new sender auto-publishes the publisher's agent and installs it for the sender |
| [Agent Management](../../application/agent_management/agent_management.md) | The Bundle tab on the agent detail page exposes all bundle management controls for developer users |
