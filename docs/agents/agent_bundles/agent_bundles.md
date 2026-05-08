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
- **Provided-by-publisher (PBP) vs Provided-by-user (PBU)** — per-spec metadata recorded on each `AgentBundleRevision.required_credential_specs` entry. A PBP spec means the publisher intends to share their own `Credential` row with installers; at install time `InstallService` validates that the publisher's row exists, still has `allow_sharing=True`, and is owned by the bundle publisher, then materialises a `CredentialShare` (publisher → installer) and links the credential to the install via `AgentCredentialLink`. A PBU spec means each installer is expected to bring their own credential, or accept a placeholder that can be filled in later. If a PBP credential is unavailable at install time the install still activates — a placeholder is created instead and `last_update_status="degraded"` is recorded. The runtime gate (see below) surfaces degraded and incomplete installs the first time the user tries to use the agent. The publisher can explicitly override the `provided_by` assignment for each linked credential from the **Credential provisioning** panel on the bundle tab — this override is persisted as `Agent.publish_settings.credential_overrides[<spec_name>].provided_by` and is consulted by `PublishService._collect_credential_specs` before falling back to the `allow_sharing`-based inference. The security invariant is preserved: an override to `"publisher"` still fails publish if the underlying `Credential` does not have `allow_sharing=True`
- **Publisher-provided AI credentials** — two optional FK columns on `AgentBundle` (`publisher_ai_credential_conversation_id`, `publisher_ai_credential_building_id`) record which of the publisher's `AICredential` rows to use for conversation and building modes respectively. At install time `InstallService` materialises an `AICredentialShare` (publisher → installer) for each non-null FK, then passes the publisher's credential id directly into `AgentEnvironmentCreate` — bypassing the installer's own AI credential selection for that mode. The `AICredentialShare` is also re-asserted on every idempotent re-install so a previously deleted share is automatically self-healed. When the installer IS the publisher, no share-with-self row is created
- **Runtime gate** — `InstallReadinessGate` runs synchronously on every user-message-to-LLM dispatch (chat, MCP, A2A, webhook session trigger) before any LLM call is made. It inspects the install's credential link state and returns one of three statuses:
  - `ready` — all credentials are in order; the message is forwarded to the LLM.
  - `needs_setup` — one or more user-provided credentials are still placeholders (the installer hasn't filled them in yet). A setup-needed reply is synthesised without engaging the LLM, and includes a link to `/agent/{id}/setup-credentials` where the installer can fill in the missing values.
  - `publisher_broken` — one or more publisher-provided credentials are missing or have had sharing revoked. The synthesised reply instructs the installer to contact the publisher; the installer can optionally replace the broken credentials from the setup page.
  
  Each channel renders the gate result in its native idiom: chat persists a `system`-role message and emits it as a normal stream event; MCP returns a structured tool reply `{response, context_id, setup_url}`; A2A synthesises a completed Task with the message and a `data` part carrying `{type: "cinna.setup_required", setup_url, missing}`; webhook session triggers log `status="setup_required"` with the structured payload in the invocation log. The LLM is never engaged when the gate blocks. When the user fills in the last placeholder, `INSTALL_SETUP_COMPLETED` fires over WebSocket and the banner on the install detail page clears automatically.
  
  Placeholder credentials for user-provided specs are filled in via the focused setup page at `/agent/{id}/setup-credentials`, accessible via the `SetupNeededBanner` shown on the agent detail page when the gate would block.

## User Stories / Flows

### Publishing a Bundle (agent-developer)

1. Developer opens an agent they own and navigates to the **Bundle tab**
2. Before publishing, the **Credential provisioning** panel (visible only on the publisher install) lets the publisher choose, per linked credential, whether it will be user-provided or publisher-provided. A toggle also controls whether the publisher provides AI credentials (conversation and/or building mode). These choices are saved immediately and consulted at the next publish step
3. Clicks "Publish" — the `PublishDialog` opens with three fields:
   - **Bundle ID** — only on the first publish; prefilled with the auto-generated value, editable. Locked once the first revision exists
   - **Version** — required; defaults to `1.0` on the first publish, otherwise suggests a minor bump from the previous revision (e.g. `1.0` → `1.1`). Manually editable for major releases
   - **Release notes** (optional)
4. On submit: workspace folders (`scripts/`, `docs/`, `knowledge/`, `files/`, `workspace_requirements.txt`, `workspace_system_packages.txt`) are copied to `<BUNDLE_STORAGE_DIR>/<bundle_id>/<revision>/`; a new `AgentBundleRevision` row is created with the user-entered `version`; `BUNDLE_PUBLISHED` WebSocket event fires
5. On first publish the `AgentBundle` row is also created; the `bundle_id` value submitted in the publish form is applied to the agent before the snapshot. Subsequent publishes append a new revision
6. All existing foreign installs receive `INSTALL_UPDATE_AVAILABLE` events (manual mode) or are marked for next-idle-cycle update (automatic mode)
7. The revision appears in the revisions list on the Bundle tab with a "current" badge — labeled `v<version>` when present, falling back to `rev <revision_number>` for legacy revisions

### Installing a Bundle (any user)

1. User opens the **Catalog** page; sees all public bundles and any bundles explicitly granted to them
2. Clicks **Install** on a catalog card
3. The **Install Page** opens — a single screen with two columns:
   - **Left (sticky)**: agent header card showing the bundle icon, display name, version, publisher, description, required credentials summary, and Bundle ID
   - **Right (scrollable)**: a setup form with an AI credentials section and a service credentials section. Each service credential spec is an accordion item labelled with its name and mode (user-provided or publisher-provided)
   - For PBP specs the accordion item is collapsed by default and shows "Shared by publisher — no action needed"
   - For PBU specs the system runs the **auto-prefill matcher**: it searches the installer's owned and shared (via `CredentialShare`) credentials for a case-insensitive `(name, type)` match. If a match is found, the item shows a suggestion ("We found `<name>` — use?") with options to accept it, skip (set up later), or pick another; the installer must make an explicit choice. If no match is found, the item is expanded and defaults to "skip — set up later". Accepted suggestions are sent as `mode="use_existing"` with the matched credential's UUID
   - When all specs are PBP and AI is publisher-provided, the form shows three short informational paragraphs and Install is one click
   - A single primary **Install** button at the bottom of the form; no "Next / Back" steps and no step indicator
4. On submit: a new `Agent` row is created seeded from the latest revision; `CredentialShare` rows are automatically created for any PBP service credentials in the revision and `AICredentialShare` rows are created for any publisher-provided AI credentials; an environment is provisioned and started using the resolved credentials; the App Data volume for `(user, bundle_id)` is created (or reattached if previously orphaned). Install never blocks on incomplete user-provided credentials — PBU specs for which the installer skipped or deferred create placeholder `Credential` rows that the runtime gate detects at first use
5. Loading state with environment progress display; on activation redirect to the install detail page. If any PBU credentials were deferred, a `SetupNeededBanner` appears on the install detail page directing the installer to the setup page

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
- **Credential specs carry metadata only** — `required_credential_specs` in a revision contains `{name, type, allow_sharing, description, provided_by, publisher_credential_id}` pairs from the publisher's linked credentials at publish time. No secret values are ever stored in a revision
- **`provided_by` resolution order** — at publish time, `PublishService._collect_credential_specs` resolves the `provided_by` value for each linked credential as follows: (1) consult `Agent.publish_settings.credential_overrides[<spec_name>].provided_by` if an override exists; (2) infer from `Credential.allow_sharing` — `True` → `"publisher"`, `False` → `"user"`. Inference is always the fallback when no override is present
- **Publisher-provided spec requires a shareable credential** — a spec can only be recorded as `provided_by="publisher"` if the underlying `Credential` row has `allow_sharing=True`. The publish-time `_validate_publisher_provides` check enforces this regardless of whether the value came from an override or inference; a publisher cannot publish a non-shareable credential as publisher-provided
- **Install request payload accepts only the typed shape** — `POST /catalog/{bundle_id}/install` no longer accepts the legacy `dict[str, str]` credentials payload. Only `dict[str, InstallCredentialSelection]` (each entry having a `mode` field) is accepted. The legacy shim that was present in Phase 3 was dropped in Phase 5; submitting the old format returns HTTP 422
- **Publisher AI credentials must be owned by the publisher** — `PATCH /bundles/{bundle_uuid}` validates that any non-null `publisher_ai_credential_conversation_id` or `publisher_ai_credential_building_id` references an `AICredential` row owned by `bundle.publisher_user_id`; the endpoint raises 400 otherwise. Setting either field to explicit `null` clears the publisher-provides state for that mode and reverts to "user provides at install time"
- **AI credential resolution order at install time** — for each mode (conversation / building) the install resolves: (1) the bundle's `publisher_ai_credential_*_id` FK if non-null; (2) the installer's explicit selection in the `InstallRequest.ai_credential_selections`; (3) `None`, leaving the env-side resolver to fall back to the installer's defaults. When the bundle provides an AI credential for a mode, the installer's selection for that mode is ignored entirely
- **Degraded install on PBP credential failure** — if a PBP service credential is unavailable at install time (deleted publisher row, `allow_sharing` revoked, credential owned by a different user than the bundle publisher), the install activates anyway: a placeholder `Credential` is created for that spec and `last_update_status="degraded"` is written on the `Agent` row. The install is not blocked; the degraded state is surfaced via the runtime gate's `publisher_broken` status the first time the installer tries to use the agent
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
| [Agent Credentials](../agent_credentials/agent_credentials.md) | `required_credential_specs` describes what credentials the bundle needs; at install time PBP specs link the publisher's existing `Credential` row via a materialised `CredentialShare` + `AgentCredentialLink`; PBU specs create placeholder `Credential` rows the installer fills in later |
| [AI Credentials](../../application/ai_credentials/ai_credentials.md) | `AgentBundle` holds two optional FK columns (`publisher_ai_credential_conversation_id`, `publisher_ai_credential_building_id`) referencing `ai_credential.id` with `ON DELETE SET NULL` semantics. At install time `AICredentialShare` rows (publisher → installer) are materialised for any non-null FK and the publisher's credential id is wired directly into the `AgentEnvironment`. Deleting a publisher's AI credential nulls out the FK via `ON DELETE SET NULL`, degrading the bundle back to "user provides" |
| [User Roles](../../application/user_roles/user_roles.md) | `publish` and `edit-bundle-id` require `agent-developer` or `admin`; catalog browse and install are available to all authenticated users |
| [Email Integration](../../application/email_integration/email_sessions.md) | `install_bundle_for_email` replaces the legacy auto-share flow; first inbound email from a new sender auto-publishes the publisher's agent and installs it for the sender |
| [Agent Management](../../application/agent_management/agent_management.md) | The Bundle tab on the agent detail page exposes all bundle management controls for developer users |
