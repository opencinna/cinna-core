# Admin-Provisioned AI Credentials + Native Account-Config — Technical Details

## File Locations

### Backend

**Models:**
- `backend/app/models/credentials/managed_ai_credential.py` — `ManagedAICredential` (parent table); `ManagedAICredentialCreate`, `ManagedAICredentialUpdate`, `ManagedAICredentialPublic` (admin DTOs); `ManagedAICredentialMember`, `ManagedAICredentialReconcileResult`, `ManagedReconcileSkip`, `ManagedReconcileBlock` **and `MANAGED_RECONCILE_BLOCK_MESSAGES`**, the reason→sentence table that sits beside the model
- `backend/app/models/credentials/managed_ai_credential_membership.py` — **new.** `ManagedAICredentialMembership` (table), `MembershipProvisioningStatus` (enum), `CONVERGEABLE_STATUSES`, `UserKeyProvisioningPublic` (owner-facing DTO), and the pair `holds_provider_key(membership)` / `holds_provider_key_clause()` — the one predicate for "does this member hold a key at the provider", in its Python and SQL forms
- `backend/app/models/credentials/provider_admin_credential.py` — **new.** `ProviderAdminCredential` (table), `ProviderAdminCredentialConfig`, `…Public`, `…Create`, `…Update`, `…VerifyResult`
- `backend/app/models/credentials/provider_adapter.py` — **new.** `ProviderAdapterPublic`, `ProviderAdaptersPublic` — the API projection of an adapter's own declarations. See [provider_adapters_tech](provider_adapters_tech.md)
- `backend/app/models/credentials/ai_credential.py` — `AICredential` (child link: `managed_credential_id` FK, ON DELETE SET NULL; existing `is_admin_managed` and `managed_by_id` columns unchanged); `AICredentialPublic` (`is_admin_managed` projection). The `AdminAICredentialPublic` / `AdminAICredentialCreate` / `AdminProvisionSkip` / `AdminAICredentialProvisionResult` DTOs were **deleted** with the legacy service (below) — no admin-facing projection of `AICredential` exists any more
- `backend/app/models/users/user.py` — `AIKeyOnboardingState` (enum: `has_key` / `preparing` / `needs_key`, **provider-agnostic**); `UserPublicWithAICredentials.api_key_onboarding_state` (**required, no default** — see [Why it is required](#why-api_key_onboarding_state-is-required-with-no-default)). The same enum is also carried per member on `ManagedAICredentialMember.api_key_onboarding_state` (**no default**)
- `backend/app/models/external/account_config.py` — `AccountConfigProviderPublic`, `AccountConfigResponse` (native-config response models)

**Routes:**
- `backend/app/api/routes/admin_llm_providers.py` — parent-oriented `POST/GET/PATCH/DELETE /admin/llm-providers/`, `POST /admin/llm-providers/{id}/set-default`, `POST /admin/llm-providers/{id}/apply-to-existing`, `POST /admin/llm-providers/test-connection`; superuser-gated. `_conflict_409(exc)` maps `ManagedCredentialConflictError` to a structured `409`
  Phase 5 adds `POST /admin/llm-providers/{managed_credential_id}/members/{user_id}/retry`
- `backend/app/api/routes/admin_provider_credentials.py` — **new.** Two routers in one module, because one configures what the other describes: `router` (prefix `/admin/provider-admin-credentials`, tag `admin-provider-credentials`) and `adapters_router` (prefix `/admin/provider-adapters`, tag `admin-provider-adapters`). Both superuser-gated. `_audit(...)` writes one `SecurityEvent` per mutation, keyed to the acting admin
- `backend/app/api/routes/ai_credentials.py` — adds `GET /ai-credentials/provisioning`, **declared above `/{credential_id}`** so the literal path is matched before the UUID route claims it
- `backend/app/api/routes/users.py` — `get_ai_credentials_status` gains `api_key_onboarding_state`; `update_user` detects an `is_active` **transition** (via `exclude_unset` + a before-snapshot, never truthiness — `is_active` defaults to True, so a truthiness check would read every silent PATCH as "activate") and calls `on_account_reactivated` / `on_account_deactivated`; `delete_user` and `delete_user_me` snapshot revocations before `session.delete` and schedule them after the commit. The AI-functions OAuth guard now asks `adapter.issues_oauth_tokens` **before** decrypting, instead of testing `expected_type == ANTHROPIC` and `startswith("sk-ant-oat")`
- `backend/app/api/routes/external_account_config.py` — `GET /external/account-config`; native-token-gated
- `backend/app/api/main.py` — routers registered (`admin_llm_providers.router`, `admin_provider_credentials.router`, `admin_provider_credentials.adapters_router`, `external_account_config.router`)

**Services:**
- `backend/app/services/credentials/managed_ai_credentials_service.py` — `ManagedAICredentialsService` (singleton: `managed_ai_credentials_service`); owns parent CRUD + reconcile + `add_members` + `apply_to_existing`; defines `ManagedCredentialConflictError` and the `MemberAddition` dataclass
- `backend/app/services/credentials/key_provisioning_service.py` — **new.** `KeyProvisioningService` (singleton: `key_provisioning_service`); owns the membership *provisioning lifecycle* — `converge`, `revoke_now`, `schedule_revocations`, `collect_user_revocations`, `suspend_user_memberships`, `resume_user_memberships`, `requeue_failed_member`, `list_user_provisionings`, `api_key_onboarding_state`; `ConvergeReport` dataclass; the `EVENT_MINTED` / `EVENT_MINT_FAILED` / `EVENT_REVOKED` / `EVENT_REVOKE_FAILED` / `EVENT_REVOKE_BLOCKED` constants
- `backend/app/services/credentials/key_provisioning_types.py` — **new.** `RevocationRequest` only. Its own module so the one real dependency between the managed-credential service and the provisioning service points in a single direction: the import cycle is a function-local import in exactly one place rather than two modules importing each other at module level
- `backend/app/services/credentials/key_provisioning_scheduler.py` — **new.** APScheduler `BackgroundScheduler`, 1-minute interval, `max_instances=1`, `coalesce=True`; `KEY_PROVISIONING_LOCK_KEY = 0x4B45594D494E54` ("KEYMINT"); submits each sweep to the main event loop via `asyncio.run_coroutine_threadsafe` with a fixed `SWEEP_WAIT_TIMEOUT_SECONDS = 180`
- `backend/app/services/credentials/provider_admin_credentials_service.py` — **new.** `ProviderAdminCredentialsService` (singleton: `provider_admin_credentials_service`); `decrypt_secret`, `usage_counts`, CRUD, `verify`, `apply_spend_limit`, `to_public`; `ProviderAdminCredentialInUseError`
- `backend/app/services/ai_providers/` — **new package.** The adapter registry every provider call now goes through. See [provider_adapters_tech](provider_adapters_tech.md)
- `admin_ai_credentials_service.py` (under `backend/app/services/credentials/`) — **deleted in phase 5.** The legacy `AdminAICredentialService` had been unwired from every route since the managed-credential model landed; it and its `AdminAICredential*` DTOs are gone
- `backend/app/services/users/account_provisioning_service.py` — `AccountProvisioningService.on_account_created` / `provision_explicit` / `on_account_deactivated` / `on_account_reactivated` / `on_account_deleted`, plus the shared `_guarded` net and `_provision` body; `ProvisioningReport`, `ProvisionedCredential` (whose `child_credential_id` is now **optional**), `ProvisioningSkip` dataclasses; `EVENT_AUTO_PROVISION` / `EVENT_AUTO_PROVISION_FAILED` constants
- `backend/app/services/users/invitation_service.py` — `_apply_reinvite_updates` calls the deactivation/reactivation hooks on an `is_active` transition. **The second (and last) such call site**, and the one a reader of `on_account_deactivated` would not think to look for
- `backend/app/core/db.py` — `leader_session(lock_key)`, the single implementation of the single-leader advisory-lock pattern. Extracted from three copies (one of which held the lock on a pooled session and leaked it — a live bug); `install_service.sweep_leader_session` and `status_repair_scheduler.repair_leader_session` are now thin wrappers over it
- `backend/app/services/users/user_service.py` — `UserService.create_account`, the single place a `User` row is built; calls `on_account_created` after the commit. See [Auth — tech](../auth/auth_tech.md#the-account-creation-chokepoint)
- `backend/app/services/external/external_account_config_service.py` — `ExternalAccountConfigService` (singleton: `external_account_config_service`)
- `backend/app/services/credentials/ai_credentials_service.py` — `update_credential` and `delete_credential` extended with `admin_override: bool = False` kwarg; `_to_public` projects `is_admin_managed`; `_clear_user_profile_for_type` used by `_clear_child_default`; **`is_shareable(credential)`** (`not credential.is_admin_managed`) and the `share_credential` refusal built on it, raising the typed `AICredentialNotShareableError`; **`owner_ids_with_a_default(session, user_ids)`**, which is what `api_key_onboarding_state` asks. **Set-shaped only — there is deliberately no single-user wrapper.** A `has_a_default(session, user_id)` convenience shipped in the fourth pass, was never called by anything, and offered exactly the shape that reintroduces an N+1 inside a loop; it was deleted in the fifth. The single-user answer is `key_provisioning_service.api_key_onboarding_state`, which delegates to the batched form

**Migrations:**
- `backend/app/alembic/versions/d3782dd039a5_add_managed_ai_credential.py` — creates `managed_ai_credential` table; adds `ai_credential.managed_credential_id` FK (ON DELETE SET NULL); `down_revision = '2f2d8e49501d'`; schema-only (no data backfill)
- `backend/app/alembic/versions/2f2d8e49501d_add_admin_managed_ai_credential.py` — earlier migration that added `is_admin_managed` and `managed_by_id` to `ai_credential`
- `backend/app/alembic/versions/b71863b32aa1_add_auto_provision_to_managed_ai_.py` — adds `auto_provision_roles`, `model_override_conversation`, `model_override_building` to `managed_ai_credential`; `down_revision = '1d737d7ef0a0'`; schema-only. Autogenerate also proposed three `cli_device_login_request` timestamp alterations — unrelated pre-existing model-vs-DB drift, deliberately excluded (applying it would drop the timezone from live rows)
- `backend/app/alembic/versions/ed8d6a23f13c_add_membership_row_provider_admin_.py` — **phase 5.** `down_revision = 'adffe56ef505'`. Creates `provider_admin_credential` and `managed_ai_credential_membership`; adds `provisioning_mode` + `provider_admin_credential_id` to `managed_ai_credential` and makes its `encrypted_data` nullable; **backfills membership rows**. The same three `cli_device_login_request` alterations were re-proposed and dropped again

### Frontend

**Route:**
- `frontend/src/routes/_layout/admin/ai-credentials.tsx` — `AdminAiCredentials` page component; `beforeLoad` redirects unauthenticated users to `/login` and non-superusers to `/`; registered at `/admin/ai-credentials`; client-side pagination (10 per page); filter by `target_user_id` via `UserAllowlistPicker` toggle-panel
- `frontend/src/routes/_layout/admin/llm-providers.tsx` — reduced to a `beforeLoad` redirect stub → `/admin/ai-credentials` (UI rename, phase 4). Deliberately carries **no auth guards of its own**: the target route runs them, and duplicating them would be a second place to keep them in step

**Sidebar entry:**
- `frontend/src/components/Sidebar/AdminMenu.tsx` — "AI Credentials" item, `Sparkles` icon, linking to `/admin/ai-credentials`

**Components under `frontend/src/components/Admin/LlmProviders/`:**
- `ManagedCredentialDialog.tsx` — unified create + edit dialog. In `create` mode it provides its own trigger button and manages open state internally; in `edit` mode it is fully controlled by the actions menu. Provider type is immutable after creation (`disabled` on the Select). API key field is blank in edit mode (blank = keep stored key for all members). Member add/remove via `UserAllowlistPicker` pre-seeded from `record.members`. Test Connection probes via `POST /test-connection` (resolves stored parent key when `api_key` is blank and `record.has_api_key` is true). Reconcile result surfaced as per-user skip/blocked toasts + summary toast. **Phase 2:** an `openedWithRef` snapshot of the seeded form values, against which every PATCH field is diffed — only changed fields travel, because the payload is absolute and resubmitting an untouched field asserts a value the admin never chose (for the auto-provision fields that is how a rename comes back as a 409 for a slot conflict the admin did not introduce). `membershipDirty` does the same for `target_user_ids`. New controls: **Modes to wire** + per-mode **model override** inputs (with `ListModelsButton`) inside the `set_user_sdk_defaults` block, and an **Auto-provision for new users** role checkbox group with the inline 409 alert. The create-time "at least one target user" guard became "at least one target user *or* auto-provision role".
- `LlmProvidersTable.tsx` — renders `ManagedAICredentialPublic[]`; columns: Name | Provider (badge) | Default provider (Yes/No badge) | Default SDK (Yes/No badge) | Auto (`AutoProvisionCell` — role chips via `userRoleLabel`, or an outline "Off" badge when the list is empty) | Shared with (member chips: `full_name <email>` or `email`) | Created; no per-member default badge; member labels resolved inline from `record.members` (members carry their own `email`/`full_name` — no separate user-fetch needed)
- `LlmProviderActionsMenu.tsx` — three-dot menu per parent row: Edit (opens `ManagedCredentialDialog` in `edit` mode), Set default for all (calls `/set-default`), **Apply to existing users** (two separate mutations — `previewMutation` with `dryRun: true`, fired unconditionally on open, and `applyMutation` — rather than one with a flag, so the preview stays on screen while the commit is in flight), Delete (with two-stage `AlertDialog`: first confirm, then if `409` escalates to a force-delete confirmation listing blocked members by name)
- `providerTypes.ts` — `PROVIDER_TYPE_OPTIONS` array (Anthropic/OpenAI/OpenAI Compatible/Google — MiniMax omitted); `getProviderTypeLabel` helper; `MANAGED_CREDENTIALS_QUERY_PREFIX = ["admin", "llm-providers"]` — **deliberately not renamed with the page**: it is a cache key, not a route path, and three surfaces (this page, the auto-provision matrix, the invite wizard) depend on producing the *same* string. Renaming it splits the cache silently — no error, just two lists that stop agreeing; `managedCredentialsQueryKey(targetUserId?)` factory. Phase 2 adds `SDK_MODE_OPTIONS` / `sdkModeLabel`, the `AutoProvisionConflict` interface, `parseAutoProvisionConflict(error)` (checks every field, not just `code`, so a future 409 shape cannot render a sentence containing `undefined`), `describeAutoProvisionConflict(conflict)` (recomposes the message with display labels), and `findAutoProvisionConflict(records, record, role)` — the advisory client-side mirror of the backend rule
- `frontend/src/components/Admin/AccessPolicy/CompanyAiCredentialsCard.tsx` — **replaces `AutoProvisionedCredentialsMatrix.tsx`** (deleted in the Access-tab redesign). Credentials × roles checkbox table, now its own full-width card on the Access tab (`lg:col-span-2` in the tab's two-column grid) rather than nested at the foot of a single policy card. Reads `managedCredentialsQueryKey()` (the same key the AI Credentials page uses, `staleTime` 30s); each toggle is a `PATCH` carrying only `auto_provision_roles`, writing the returned row straight into the cache before invalidating so a second toggle can never compute `next` off a stale list; capped at **5 rows** (granted-first, then alphabetical) with a footer link to `/admin/ai-credentials` reading "Manage AI credentials" or "Show all (N) on AI Credentials"; the whole table disables while a toggle is in flight (a second click would be a second full reconcile and a second audit event for one intended change); the 409 renders as a destructive `Alert` under the table, naming both the cell clicked and the credential that owns the conflicting default. See [Access Policy — tech](../server_configuration/access_policy_tech.md)
- `frontend/src/utils/userRoles.ts` — **new.** `USER_ROLE_OPTIONS` (capability order: agent-user → agent-developer → admin) and `userRoleLabel(role)`. Takes a bare `string`, not `UserRoleValue`, so a server that learns a fourth role renders its identifier instead of crashing. `NewUserDefaultsCard`, `LlmProvidersTable`, `LlmProviderActionsMenu`, the dialog and `CompanyAiCredentialsCard` all read from here
- `frontend/src/components/Common/ListModelsButton.tsx` — gains an optional `probeModels?: () => Promise<AICredentialTestResult>` prop. When given, `credentialId` / `credentialType` are unused and the caller gates the button with `disabled`. Needed because the admin dialog holds a *parent record on a different endpoint*, or a key typed into the form and never persisted — neither is an `AICredential` id. The dialog passes a probe that does **not** go through the shared Test Connection mutation, so opening the picker never repaints the Test Connection banner and the picker's own Retry cannot re-enter a pending mutation

**Phase 5 frontend additions:**
- `frontend/src/routes/_layout/admin/ai-credentials.tsx` — the page becomes **two tabs** on the one URL (`"managed"` / `"provider-keys"`). The tab is local `useState`, not a search param, so there is no per-tab URL. Header actions are tab-dependent. The managed list polls every 10s while `records.some(hasKeyInFlight)` and the managed tab is active
- `frontend/src/components/Admin/ProviderAdminCredentials/ProviderAdminCredentialsTable.tsx` — **new.** The Provider keys tab. Row menu: Verify / Apply spend limit / Edit / Disconnect. Disconnect sends `force=false` first; a `409` whose `detail.code === "provider_admin_credential_in_use"` is caught, rendered inline with both counts, and relabels the button **"Disconnect anyway"**
- `frontend/src/components/Admin/ProviderAdminCredentials/ProviderAdminCredentialDialog.tsx` — **new.** Name / Provider (from `mintingAdapters` only, immutable in edit) / Admin API key (required on create; edit placeholder "Leave blank to keep the stored key"), then **fields rendered dynamically from the adapter's `admin_config_schema`**. Validation messages are generic and built from each field's own label. When the adapters query errors the save button is disabled — the form refuses to guess a schema it could not load
- `frontend/src/components/Admin/LlmProviders/useProviderAdapters.ts` — **new.** `PROVIDER_ADAPTERS_QUERY_KEY`, `useProviderAdapters(enabled)`, and `adminConfigFields(adapter)` (parses `admin_config_schema.fields`, dropping malformed entries). `supportsMinting` / `canMintNow` compare `=== true`, so *loading* is false rather than optimistically permissive
- `frontend/src/components/Admin/LlmProviders/MemberKeyStatus.tsx` — **new.** `hasKeyInFlight(record)`, `<KeyStatusSummary>` (the **Keys** column: "Shared key", or a per-status roll-up with a spinner), `<MemberChip>` (member pill + status label + failure reason + **Retry**, calling `retryMemberKeyProvisioning`)
- `frontend/src/components/UserSettings/KeyProvisioningRows.tsx` — **new.** `useMyKeyProvisionings()` (polls every 10s **only while something is in flight**, and invalidates the credential list and status queries when the in-flight count drops) and the two owner-facing rows
- `frontend/src/utils/keyProvisioning.ts` — **new.** `MY_KEY_PROVISIONINGS_QUERY_KEY`, `MEMBERSHIP_STATUS_META` (an exhaustive `Record<MembershipProvisioningStatus, …>` carrying an admin label, an owner label, a tone and `inFlight` — the exhaustive record is what makes a new server status a **type error** rather than a blank cell), and `describeProvisionError(code)` (unknown codes render verbatim rather than vanishing)
- `ManagedCredentialDialog.tsx` — the **Key source** radio group (disabled in edit mode), the conditional **Provider organisation** select, the disappearing API-key field, and three new props (`initialTargets`, `nameSubject`, `onCreated`) so the invite success screen can seed it. `isControlled` replaces `mode === "edit"` for open-state ownership
- `InviteSuccessPanel.tsx` — **replaces the nested "Add a key for this user" card.** A guideline-driven redesign of the invite success screen moved this from a card opening `ManagedCredentialDialog` in place to a plain `Button variant="link"` reading "Add an AI key for `<email>`", navigating to `/admin/ai-credentials?newCredentialFor=<user.id>&label=<label>` (hidden for an inactive account, same condition as before)
- `routes/_layout/admin/ai-credentials.tsx` — reads `newCredentialFor` / `label` off `validateSearch`, opens `ManagedCredentialDialog` **controlled** with `initialTargets=[{id, userId, fallbackLabel: label}]` and `nameSubject=label` when present, then strips both params (the `?new=1` latch idiom from `credential/$credentialId.tsx`, re-armed per target id so a second hand-off to a different person opens again). `onCreated` sets a one-line result under the page header from the created record's own member row (`api_key_onboarding_state`): `has_key` → "Key added."; `preparing` → "The key is being created now."; `needs_key` → an amber alert, "Key added — not their default."
- `routes/_layout/index.tsx` — the dashboard wall reads `api_key_onboarding_state`, not `has_anthropic_api_key`. It polls every 10s until the answer is `has_key` (both `preparing` **and** `needs_key` poll — see [The dashboard wall](#the-dashboard-wall-latch-banner-and-polling)), and the full-page form is **latched to first render** via `wallAllowedRef`. **There is no loading fallback** — the field is required on the server's response, so the browser reads `credentialsStatus?.api_key_onboarding_state` with no `?? "needs_key"` and `undefined` means only "the server has not answered"

**Generated client services used:**
- `AdminLlmProvidersService` — `createManagedAiCredential`, `listManagedAiCredentials`, `getManagedAiCredential`, `updateManagedAiCredential`, `deleteManagedAiCredential`, `setManagedAiCredentialDefault`, `applyManagedAiCredentialToExisting`, `testManagedAiCredentialConnection`, `retryMemberKeyProvisioning`
- `AdminProviderCredentialsService` — `listProviderAdminCredentials`, `createProviderAdminCredential`, `getProviderAdminCredential`, `updateProviderAdminCredential`, `deleteProviderAdminCredential`, `verifyProviderAdminCredential`, `applyProviderSpendLimit`
- `AdminProviderAdaptersService` — `listProviderAdapters`
- `AiCredentialsService` — `listMyKeyProvisionings`

**Frontend leftovers, recorded rather than tidied:**
- `ManagedCredentialDialog` still states the per-provider required-field rules **three times** — the zod enum, the `superRefine` `openai_compatible` branch, and the `showBaseUrl` / `showModel` conditions — while `requires_base_url` / `requires_model` now arrive on `ProviderAdapterPublic` and are consumed nowhere
- `PROVIDER_TYPE_OPTIONS` / `PROVIDER_TYPE_LABELS` in `providerTypes.ts` still hold a hardcoded provider list. **Deliberate:** driving the picker from the adapters endpoint would re-expose MiniMax, which the UI hides on purpose
- Provider types are also hardcoded in `UserSettings/AICredentialDialog.tsx`, `UserSettings/AICredentials.tsx`, `Environments/EnvironmentCard.tsx` and `Environments/EnvironmentConfigForm.tsx` — pre-existing, untouched by this phase

**Also implemented (admin-curated model list):**
- `ManagedCredentialDialog.tsx` — "Default model" text input (with a "View available models ↗" external link next to the label, pointing to the provider's official models docs — `PROVIDER_MODELS_DOC_URL` map; omitted for `openai_compatible`) + "Available models" multi-line textarea; **"Fill top 10 models"** button (replaces the old "Use models from test" label): auto-runs Test Connection if no fresh successful result is cached, then fills "Available models" with the top 10 discovered models and auto-sets "Default model" via `pickDefaultModel` (Google → `GOOGLE_DEFAULT_MODEL = "gemini-flash-latest"`; Anthropic → `pickHighestSonnet` highest version Sonnet from the list; OpenAI/OpenAI Compatible → first model); edit-mode seeding; `None` vs `[]` clear semantics on submit; `stripProviderPrefix` + `parseAvailableModels` client-side normalization for display
- `EnvironmentConfigForm.tsx` — model-override `<datalist>` prefers `available_models` over `discovered_models` when non-empty
- `AICredentials.tsx` — model-override `<datalist>` same preference; read-only "Default model: …" line rendered on admin-managed credential entries when `default_model` is set

**Still pending (not yet implemented):**
- Native-app (Cinna Desktop / Mobile) provider and chat-mode auto-creation driven by `GET /external/account-config`

---

## Database Schema Changes

### `managed_ai_credential` table (new — migration `d3782dd039a5`)

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `id` | `UUID` | PK | Parent record identity |
| `name` | `VARCHAR(255)` | NOT NULL | Human-readable label |
| `type` | `VARCHAR(50)` | NOT NULL | `AICredentialType` enum value |
| `encrypted_data` | `TEXT` | **nullable** (was NOT NULL; relaxed by `ed8d6a23f13c`) | Fernet-encrypted JSON `{api_key, base_url?, model?}` — the canonical key in `shared` mode. **NULL in `minted` mode**, where the parent holds no key at all. Every reader goes through `_decrypt_parent`, which refuses a minted parent rather than dereferencing the NULL |
| `provisioning_mode` | `VARCHAR(24)` | NOT NULL, server_default `'shared'` | `shared` \| `minted`. Added by `ed8d6a23f13c`. The server default is what makes the backfill correct: every pre-existing record is a shared-key record, and no code path existed to write `'minted'` before this migration |
| `provider_admin_credential_id` | `UUID` | nullable, FK → `provider_admin_credential.id` ON DELETE **SET NULL** (`fk_managed_ai_credential_provider_admin_credential`) | The admin secret this record's per-user keys are minted (and revoked) with. Required in `minted` mode, NULL in `shared`. SET NULL rather than CASCADE for the same reason as `managed_by_id`: losing the pointer must degrade minting, not delete the record and its members' keys. The service refuses to delete a provider admin credential any parent still points at, so this is a safety net rather than a normal path |
| `base_url` | `VARCHAR(500)` | nullable | Non-secret mirror for projection/UI |
| `model` | `VARCHAR(255)` | nullable | Non-secret mirror for projection/UI |
| `default_model` | `VARCHAR(255)` | nullable | Admin-curated default model (bare concrete id). Added by migration `c1a4b2d3e5f6`. |
| `available_models` | `JSON` | nullable | Admin-curated selectable model list. Added by migration `c1a4b2d3e5f6`. |
| `set_as_default` | `BOOLEAN` | NOT NULL, server_default false | Whether each child is set as its owner's default |
| `set_user_sdk_defaults` | `BOOLEAN` | NOT NULL, server_default false | Whether each owner's SDK-default pointers are wired |
| `sdk_default_modes` | `JSON` | NOT NULL, server_default `["conversation","building"]` | Modes to wire |
| `auto_provision_roles` | `JSON` | NOT NULL, server_default `'[]'::json` | Roles whose newly created accounts receive this credential. Added by migration `b71863b32aa1`. The empty backfill is the only safe one — a permissive default would hand a company key to the next person who signs up. |
| `model_override_conversation` | `VARCHAR(255)` | nullable | Model pinned on each member's `User.default_model_override_conversation` for the conversation mode. Added by `b71863b32aa1`. |
| `model_override_building` | `VARCHAR(255)` | nullable | Same for building. Added by `b71863b32aa1`. |
| `expiry_notification_date` | `TIMESTAMP` | nullable | Informational expiry reminder |
| `managed_by_id` | `UUID` | nullable, FK → `user.id` ON DELETE SET NULL, index `ix_managed_ai_credential_managed_by` | Which admin owns/manages this record; NULL when the admin account is deleted |
| `created_at` | `TIMESTAMP` | NOT NULL | Creation time (UTC) |
| `updated_at` | `TIMESTAMP` | NOT NULL | Last update time (UTC) |

### `ai_credential` table — columns added across two migrations

**Migration `d3782dd039a5`:**

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `managed_credential_id` | `UUID` | nullable, FK → `managed_ai_credential.id` ON DELETE SET NULL, index `ix_ai_credential_managed_credential` | Structural link to the parent. NULL = not a managed child, or parent was deleted out-of-band |

**Migration `c1a4b2d3e5f6`:**

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `default_model` | `VARCHAR(255)` | nullable | Mirror of parent value, written through by reconcile. Read by SDK resolution + native config. NULL for self-created credentials. |
| `available_models` | `JSON` | nullable | Mirror of parent value, written through by reconcile. Read by model pickers + native config. NULL for self-created credentials. |

The `is_admin_managed` and `managed_by_id` columns already exist from migration `2f2d8e49501d`.

No data backfill in any migration — existing rows get `NULL` (preserves current behavior: catalog default + discovered list).

Downgrade for `d3782dd039a5`: drops FK `fk_ai_credential_managed_credential`, drops index `ix_ai_credential_managed_credential`, drops column `managed_credential_id`, drops index `ix_managed_ai_credential_managed_by`, drops table `managed_ai_credential`.

Downgrade for `c1a4b2d3e5f6`: drops `ai_credential.available_models`, `ai_credential.default_model`, `managed_ai_credential.available_models`, `managed_ai_credential.default_model` (in that order).

Downgrade for `b71863b32aa1`: drops `model_override_building`, `model_override_conversation`, `auto_provision_roles` (in that order).

### `managed_ai_credential_membership` table (new — migration `ed8d6a23f13c`)

One row per `(parent, user)`. **The only definition of "who is a member"** — the previous derivation from the children is gone, not parallel.

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `id` | `UUID` | PK | |
| `managed_credential_id` | `UUID` | NOT NULL, FK → `managed_ai_credential.id` ON DELETE **CASCADE**, indexed | The parent |
| `user_id` | `UUID` | NOT NULL, FK → `user.id` ON DELETE **CASCADE** | The member |
| `status` | `VARCHAR(24)` | NOT NULL, **no application-layer default** | One of `MembershipProvisioningStatus`. Every writer states which one it means; nobody reads a NULL and interprets it |
| `ai_credential_id` | `UUID` | nullable, FK → `ai_credential.id` ON DELETE **SET NULL** | The child this membership materialised, when one exists. SET NULL rather than CASCADE: deleting the key must not delete the membership, because a suspended or failed member is still a member |
| `external_key_ref` | `JSON` | nullable | Provider handles for a minted key. Written **before** the key is stored, so a process that dies mid-mint leaves the handles needed to revoke the orphan rather than a leaked key nobody can name. NULL on every shared membership |
| `provision_attempts` | `INTEGER` | NOT NULL, server_default `0` | Bounded by `KeyProvisioningService.MAX_ATTEMPTS` |
| `next_attempt_at` | `TIMESTAMPTZ` | nullable | When the next attempt becomes eligible. NULL means "eligible now" for a converge-able status, and means nothing at all for a terminal one — which is why the **status** is what a reader consults, never this column |
| `last_error` | `TEXT` | nullable | Coarse reason code (`mint_failed`, `project_not_capped`, `no_admin_credential`, …). Never a provider response body, never key material |
| `created_at` / `updated_at` | `TIMESTAMP` | NOT NULL | |

Constraints and indexes:

- `uq_managed_ai_credential_membership_parent_user` on `(managed_credential_id, user_id)` — what makes "is this person a member" a single-row question with a single answer, and the guard the backfill relies on
- `ix_managed_ai_cred_membership_status_next` on `(status, next_attempt_at)` — the converge query's index
- `ix_managed_ai_cred_membership_user` on `(user_id)` — the per-user reads (owner-facing list, onboarding state, the account-lifecycle cascade)

#### The backfill, and what it asserts

The risky step of `ed8d6a23f13c`. It turns the old derivation into rows, once, after which the derivation is gone. A backfill that quietly drops a member produces an install where somebody's credential is invisible to the admin UI and to every later reconcile — a failure nobody notices until that person has no key. So its three assumptions are checked rather than trusted:

| Assumption | How it is upheld |
|-----------|------------------|
| Every parent at this revision is shared-mode | True because `provisioning_mode` is created in the same migration. Every backfilled row is therefore `status='not_applicable'` with a NULL `external_key_ref` |
| A membership is exactly `(managed_credential_id, owner_id)` | A **pre-check** selects duplicate `(parent, owner)` pairs and raises naming them. The unique constraint would reject them anyway; the pre-check rejects them *first*, with a message. **Loud failure, never a skipped row** — there is no correct winner to pick |
| A child with a NULL `managed_credential_id` is a member of nothing | A classification, not an omission: the FK is `ON DELETE SET NULL` and such rows are documented as orphans that degrade to plain `is_admin_managed` credentials. Excluded deliberately |

A **post-check** compares `result.rowcount` for the INSERT against the eligible-children count and raises on a mismatch, so a partial insert fails the migration instead of shipping a short member list. The count deliberately measures *the rows this statement inserted*, not the table's total — the two are the same number today only because the table is created in the same migration, and the weaker check would go on passing while measuring the wrong thing.

`gen_random_uuid()` is used for the ids: built into PostgreSQL 13+ with no `pgcrypto` extension, and this stack runs 17. It is the first use of it in this tree.

### `provider_admin_credential` table (new — migration `ed8d6a23f13c`)

Server-scoped, **no `owner_id`**. The shape is copied deliberately from `MailServerConfig`: server-scoped with an encrypted secret column, a public projection exposing `has_secret: bool` instead of the value, and a superuser-or-nothing router.

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `id` | `UUID` | PK | |
| `name` | `VARCHAR(255)` | NOT NULL | Human label |
| `provider_type` | `VARCHAR(50)` | NOT NULL, index `ix_provider_admin_credential_type` | `AICredentialType` value |
| `encrypted_secret` | `TEXT` | NOT NULL | Fernet-encrypted administration secret. **Never projected, never linked to an environment, never in `/external/account-config`** |
| `config` | `JSON` | NOT NULL, server_default `'{}'::json` | `ProviderAdminCredentialConfig` — `organization_id?`, `project_id?`, `spend_limit_cents` |
| `last_verified_at` | `TIMESTAMPTZ` | nullable | Last successful Verify |
| `last_verify_error` | `TEXT` | nullable | Coarse reason code for the last failed Verify. Both are non-secret and both are shown to the admin |
| `created_by_id` | `UUID` | nullable, FK → `user.id` ON DELETE **SET NULL** | Audit only. **Never CASCADE** — user deletion is a bare cascade, and deleting the superuser who pasted the key must not destroy the instance's ability to revoke every key it ever minted |
| `created_at` / `updated_at` | `TIMESTAMP` | NOT NULL | |

**Why a separate table rather than a flag on `ai_credential`.** Two existing readers are correct for the table they read and would both reach this secret the day it lived there: `model_discovery_service` selects *every* `ai_credential` row on a cron and calls the provider with each key, and `external_account_config_service` hands *every* row a user owns, decrypted, to the desktop client. The isolation is structural rather than intentional — sharing (`ai_credential_share.ai_credential_id`), environment linking (`agent_environment.{conversation,building}_ai_credential_id`), bundle publisher wiring and the blast-radius counts are all foreign keys pinned to `ai_credential.id`; the environment credential bag is a fixed slot dict with no slot to pour into; and nothing iterates `SQLModel.metadata`.

---

## Parent DTOs

### `ManagedAICredentialCreate`

| Field | Type | Notes |
|-------|------|-------|
| `name` | `str` | 1–255 chars |
| `type` | `AICredentialType` | Provider type; immutable after creation |
| `api_key` | `str \| None` | Plaintext key (min length 1 when present); encrypted into the parent row; written to children at add time. **Required in `shared` mode, refused in `minted` mode.** Nullable rather than required-with-a-sentinel so the omission is representable in the request model itself; the *service* raises the 400 that ties it to `provisioning_mode`, because that rule is a policy and policies live in one place |
| `provisioning_mode` | `ProvisioningMode` | Default `shared`. Immutable after creation |
| `provider_admin_credential_id` | `uuid.UUID \| None` | Required in `minted` mode — the admin secret the keys are minted with. Must exist and must be for the same provider |
| `base_url` | `str \| None` | Required for `openai_compatible`; optional for `google`; max 500 chars |
| `model` | `str \| None` | Required for `openai_compatible`; max 255 chars |
| `default_model` | `str \| None` | Admin-curated default model (bare concrete id, max 255); normalized server-side (strip `provider/` prefix, trim) |
| `available_models` | `list[str] \| None` | Admin-curated selectable model list; normalized server-side; `None` = no curation |
| `expiry_notification_date` | `datetime \| None` | Informational expiry reminder |
| `target_user_ids` | `list[uuid.UUID]` | **May be empty** (phase 2 dropped the `min_length=1`): an auto-provision-only credential legitimately starts with no members. Deduplicated preserving order |
| `set_as_default` | `bool` | Default `False` |
| `set_user_sdk_defaults` | `bool` | Default `False` |
| `sdk_default_modes` | `list[str]` | Default `["conversation", "building"]`. **Not validated** — see Known Gaps in the [business doc](admin_ai_credential_provisioning.md#known-gaps) |
| `auto_provision_roles` | `list[str]` | Default `[]`. Normalized + validated against `VALID_AUTO_PROVISION_ROLES` (`= tuple(VALID_USER_ROLES)`); an unknown entry is a `400` |
| `model_override_conversation` | `str \| None` | Max 255; normalized through `_normalize_default_model` (trim, strip `provider/`, blank → `None`) |
| `model_override_building` | `str \| None` | Same |

### `ManagedAICredentialUpdate`

All fields optional (partial update). Omitting `api_key` keeps the stored key. Omitting `target_user_ids` leaves membership unchanged. Omitting `available_models` leaves the stored curation unchanged; sending `[]` clears it.

| Field | Type | Notes |
|-------|------|-------|
| `name` | `str \| None` | |
| `api_key` | `str \| None` | Non-None triggers key rotation + Update pass for all current members. **On a `minted` record it is a `400`, not a silent no-op** — there is no stored key to replace, and silently accepting a rotation that rotates nothing is how an admin comes to believe they have rolled a key they have not |
| `base_url` | `str \| None` | |
| `model` | `str \| None` | |
| `default_model` | `str \| None` | `None` = no change; blank string normalized to `None` (effectively clears). Normalized server-side. |
| `available_models` | `list[str] \| None` | `None` = no change; `[]` = explicit clear (fall back to `discovered_models`). Normalized server-side. |
| `expiry_notification_date` | `datetime \| None` | |
| `target_user_ids` | `list[uuid.UUID] \| None` | When supplied, the reconcile diff is against this list |
| `set_as_default` | `bool \| None` | |
| `set_user_sdk_defaults` | `bool \| None` | |
| `sdk_default_modes` | `list[str] \| None` | |
| `auto_provision_roles` | `list[str] \| None` | `None` = no change; `[]` = stop auto-provisioning (**not** a revoke — existing members keep their credential) |
| `model_override_conversation` | `str \| None` | Three-valued: omitted/`null` = unchanged; `""` = clear to NULL **and** retract the pin from members still carrying the dropped value; a model id = set + write through. This is why the field is not `str \| None` with `min_length=1` — the empty string is meaningful here, not malformed |
| `model_override_building` | `str \| None` | Same |

### `ManagedAICredentialPublic`

Never includes `encrypted_data` or key material.

| Field | Type | Notes |
|-------|------|-------|
| `id` | `uuid.UUID` | |
| `name` | `str` | |
| `type` | `AICredentialType` | |
| `base_url` | `str \| None` | |
| `model` | `str \| None` | |
| `default_model` | `str \| None` | Admin-curated default model (bare concrete id) |
| `available_models` | `list[str] \| None` | Admin-curated selectable model list |
| `set_as_default` | `bool` | |
| `set_user_sdk_defaults` | `bool` | |
| `sdk_default_modes` | `list[str]` | |
| `auto_provision_roles` | `list[str]` | Roles whose new accounts receive this credential |
| `model_override_conversation` | `str \| None` | |
| `model_override_building` | `str \| None` | |
| `expiry_notification_date` | `datetime \| None` | |
| `managed_by_id` | `uuid.UUID \| None` | Which admin manages this; NULL when that admin was deleted |
| `provisioning_mode` | `ProvisioningMode` | **Required, no default.** An absent mode rendered through a client-side `!== "minted"` fallback is the browser deciding a record holds one shared key because a field did not arrive |
| `provider_admin_credential_id` | `uuid.UUID \| None` | Which organisation this record mints through |
| `has_api_key` | `bool` | Whether this parent holds a key of its own. `True` for every shared record, **`False` for every minted one** — which is why it is computed rather than the constant it used to be. A reader who trusts the old "always true" comment concludes a minted parent's key is *missing* rather than absent by design |
| `is_oauth_token` | `bool` | Derived from the stored key prefix (`sk-ant-oat`) for Anthropic; `False` for all other types |
| `members` | `list[ManagedAICredentialMember]` | One entry per **membership row**, not per child credential |
| `member_count` | `int` | `len(members)` |
| `created_at` | `datetime` | |
| `updated_at` | `datetime` | |

### `ManagedAICredentialMember`

| Field | Type | Notes |
|-------|------|-------|
One member = one membership row, **not** one child credential. On a minted record a person is a member from the moment the admin adds them, and their key exists a little later or not at all.

| Field | Type | Notes |
|-------|------|-------|
| `user_id` | `uuid.UUID` | The member |
| `email` | `str` | Member's email |
| `full_name` | `str \| None` | Member's full name |
| `child_credential_id` | `uuid.UUID \| None` | The `AICredential.id`. **`None` for exactly the statuses that mean "no key exists right now"** (`pending`, `minting`, `failed`, `suspended`); set for the two that mean one does (`not_applicable`, `provisioned`) |
| `is_default` | `bool` | Whether this child is the owner's default for its type |
| `provisioning_status` | `MembershipProvisioningStatus` | **Required, no default.** A default here would be optional on the wire, and a client reading an absent status through a fallback is a client asserting a provisioning policy it inferred from a missing field. The client reads this one field and never reconstructs the state from which other fields happen to be null |
| `provision_error` | `str \| None` | Coarse failure reason for a failed or retrying member. Never a provider response body, never key material |
| `provision_attempts` | `int` | Default `0` |
| `api_key_onboarding_state` | `AIKeyOnboardingState` | **Required, no default.** The *server's* answer for this person — the same field, from the same predicate, the dashboard wall reads. It is here so an admin surface that just created a credential is told what the member will actually experience rather than inferring success from having written a row: `set_as_default` defaults to `False`, so a perfectly good credential can leave its owner on the paste-a-key wall with that exact credential listed in their settings. Computed in batch by `_owner_key_states` → `key_provisioning_service.api_key_onboarding_states` and passed into `_member_dto(..., key_state=…)` as a required keyword |

### `MembershipProvisioningStatus`

`not_applicable` \| `pending` \| `minting` \| `provisioned` \| `failed` \| `suspended`. Semantics in the [business doc](admin_ai_credential_provisioning.md#membership-statuses). `CONVERGEABLE_STATUSES = (pending, minting)` — everything else is terminal for the converge pass.

### `ProvisioningMode`

`shared` \| `minted`. Set at creation, immutable afterwards.

### `UserKeyProvisioningPublic` (owner-facing)

The counterpart of `ManagedAICredentialMember` for the person the key is being made *for*, and the only way somebody can be told a key is on its way.

| Field | Type | Notes |
|-------|------|-------|
| `managed_credential_id` | `uuid.UUID` | |
| `name` | `str` | What the administrator called the record |
| `type` | `AICredentialType` | **Typed, not a bare string** — the admin projection publishes a union here, and an owner-facing `string` would be a second, weaker answer to the same question in the generated client |
| `status` | `MembershipProvisioningStatus` | Only `pending`, `minting`, `failed` are ever projected here |
| `last_error` | `str \| None` | Coarse reason code |
| `updated_at` | `datetime` | |

It has to be its own projection rather than an extra row in the credential list, because that list is `AICredentialPublic` and **every `AICredential` row that exists is usable** — an entry there with no key would break the invariant every consumer of that table relies on.

### `ProviderAdminCredential*` DTOs

| DTO | Notes |
|-----|-------|
| `ProviderAdminCredentialConfig` | `organization_id?`, `project_id?`, `spend_limit_cents` (`ge=1`, **integer cents**). `project_id` may be an existing project or left empty for setup to create one; either way the project is verified to have an *enforcing* limit before the first key is minted |
| `ProviderAdminCredentialCreate` | `name`, `provider_type`, `secret` (min length 1), `config` |
| `ProviderAdminCredentialUpdate` | All optional; **omitting `secret` keeps the stored one**, so renaming a record never round-trips a secret |
| `ProviderAdminCredentialPublic` | `id`, `name`, `provider_type`, `config`, `has_secret: bool` (never the value), `last_verified_at`, `last_verify_error`, `created_by_id`, `minting_credential_count`, `live_minted_key_count`, `delete_blocked`, timestamps. `delete_blocked` is the **answer**; the two counts are the explanation. A client computing `(a or b) > 0` itself would be re-deriving a server policy in the browser and would keep answering the old way the day the rule changes |
| `ProviderAdminCredentialVerifyResult` | `ok`, `account_ref` (provider-side identity — never key material), `spend_limit_enforcing`, `spend_limit_cents`, `error`. Two questions in one answer object because they fail independently |

### `ProviderAdapterPublic` / `ProviderAdaptersPublic`

Documented with the registry in [provider_adapters_tech](provider_adapters_tech.md#get-apiv1adminprovider-adapters).

### `ManagedAICredentialReconcileResult`

| Field | Type | Notes |
|-------|------|-------|
| `record` | `ManagedAICredentialPublic` | The parent record as it stands after reconcile |
| `added` | `list[ManagedAICredentialMember]` | Newly created children |
| `removed` | `list[uuid.UUID]` | Owner IDs whose children were successfully deleted |
| `updated` | `list[ManagedAICredentialMember]` | Members whose child was actually mutated this reconcile (empty on no-op) |
| `updated_count` | `int` | `len(updated)` — convenience scalar |
| `skipped` | `list[ManagedReconcileSkip]` | Users skipped (unknown/inactive/provision_failed/update_failed) |
| `blocked` | `list[ManagedReconcileBlock]` | Members whose removal was blocked — Tier-2 blast-radius (`in_use_bundle`), `remove_failed`, or **`mint_in_flight`** |

### `ManagedReconcileSkip`

| Field | Type | Values |
|-------|------|--------|
| `user_id` | `uuid.UUID` | The skipped target |
| `reason` | `str` | `"user_not_found"` / `"user_inactive"` / `"provision_failed"` / `"update_failed"` |

### `ManagedReconcileBlock`

| Field | Type | Notes |
|-------|------|-------|
| `user_id` | `uuid.UUID` | The blocked member |
| `reason` | `str` | `"in_use_bundle"` / `"mint_in_flight"` / `"remove_failed"` |
| `message` | `str` | **Required, no default.** The blocked *reason sentence*, server-authored, rendered verbatim by every consumer |
| `impact` | `dict \| None` | Deletion-impact payload from `AICredentialInUseError.impact` |

#### One sentence, one source

`message` is filled from `MANAGED_RECONCILE_BLOCK_MESSAGES`, a `dict[str, str]` declared **beside the model** in the same module:

| `reason` | Sentence |
|---|---|
| `in_use_bundle` | "Their credential is in use by a published bundle. Removing them anyway degrades that bundle back to \"user provides\"." |
| `mint_in_flight` | "A key is being created for them right now. Try again in a moment — forcing it through does not help and is not needed." |
| `remove_failed` | "Removing their credential failed unexpectedly. It has been logged; try again." |
| *(anything else)* | "This member could not be removed." — the `of()` fallback |

**`message` being required, with no default, is what makes `ManagedReconcileBlock.of(...)` the only practical constructor**, and `of` is what consults the table. Membership of the table is also the validity test for a reason string: a fourth reason cannot be introduced without a sentence to go with it.

**What this replaced, and why it mattered.** The DTO used to carry only `reason`, and each of the three consumers substituted its own copy of the constant "in use by a published bundle". So an admin blocked by an **in-flight mint** was told they had a bundle conflict — and the remedy for a bundle conflict is `force=true`, which on that path strands a live provider key. Three renderers, one wrong sentence each, and the wrong sentence pointed at a destructive action.

The three consumers now render `message` verbatim:

| Consumer | Where |
|---|---|
| The `409` body | `admin_llm_providers.py` — `" ".join(["One or more members could not be removed."] + distinct)`, where `distinct` is `dict.fromkeys(b.message for b in result.blocked)` (deduped, order-preserving), plus the full `blocked` list |
| The force-delete confirmation | `LlmProviderActionsMenu.tsx` — appends `` — ${b.message}`` under each blocked user's name; the toast that opens it stays generic ("Review below before forcing") |
| The member-dialog toast | `ManagedCredentialDialog.tsx` — `` `${labelFor(b.user_id)} was not removed. ${b.message}` `` |

### `ManagedAICredentialApplyCandidate`

A user who *would* receive the credential on apply-to-existing. Deliberately **not** a `ManagedAICredentialMember`: a member is identified by the child credential it owns, and on a dry run no child exists — inventing a placeholder id would be a lie the frontend could not distinguish from a real member.

| Field | Type | Notes |
|-------|------|-------|
| `user_id` | `uuid.UUID` | |
| `email` | `str` | |
| `full_name` | `str \| None` | |
| `role` | `str` | The role that matched `auto_provision_roles` |

### `ManagedAICredentialApplyResult`

Subclasses `ManagedAICredentialReconcileResult` (so `record` / `added` / `skipped` mean exactly what they do elsewhere) and adds:

| Field | Type | Notes |
|-------|------|-------|
| `dry_run` | `bool` | `True` when nothing was written |
| `candidate_count` | `int` | Populated on **both** paths so the confirm dialog and the result toast quote the same number |
| `candidates` | `list[ManagedAICredentialApplyCandidate]` | Populated on a dry run only; empty on a real run |
| `defaults_overwrite_count` | `int` | Candidates who lose *something* to this grant, counted **once** per candidate however many slots they lose. Covers **both** default axes: `set_user_sdk_defaults` (a claimed mode whose credential pointer **or** model override is non-NULL) **and** `set_as_default` (the candidate already holds a default `AICredential` of the parent's type). `0` only when the record wires no defaults at all — **not** whenever `set_user_sdk_defaults` is false |

### `MemberAddition` (dataclass, service-internal)

What `add_members` returns: `added: list[ManagedAICredentialMember]` + `skipped: list[ManagedReconcileSkip]`. Deliberately **not** a `ManagedAICredentialReconcileResult` — that shape carries `removed`/`blocked`/`updated`, which an add-only operation can never populate, and a `record` projection all three callers throw away. Building it would have put `_to_public`'s per-member user lookup and its parent-key decrypt on the signup request path, for a value nobody reads.

### `ManagedCredentialConflictError` (exception)

Raised by `_validate_auto_provision_uniqueness`. Carries `conflicting_name`, `conflicting_id`, `role`, `mode`; `_conflict_409` in the route turns it into:

```json
{"code": "auto_provision_conflict", "message": "…",
 "conflicting_credential_id": "…", "conflicting_credential_name": "…",
 "role": "agent-developer", "mode": "building"}
```

Structured rather than prose because the frontend highlights the offending role/mode cell and links to the other record, and it cannot do either from a sentence.

---

## API Endpoints

### Admin LLM Providers (`/api/v1/admin/llm-providers`)

**File:** `backend/app/api/routes/admin_llm_providers.py`
**Auth gate:** `get_current_active_superuser` — `403` for anyone else

| Method | Path | Request | Response | Notes |
|--------|------|---------|----------|-------|
| `POST` | `/admin/llm-providers/` | `ManagedAICredentialCreate` | `ManagedAICredentialReconcileResult` | Create parent + initial reconcile; bad per-type payload → `400` |
| `GET` | `/admin/llm-providers/` | query `?managed_by_id=` & `?target_user_id=` (both optional) | `list[ManagedAICredentialPublic]` | Fleet-wide, ordered by `created_at DESC`; filtered when params supplied |
| `GET` | `/admin/llm-providers/{id}` | — | `ManagedAICredentialPublic` | `404` if not found |
| `PATCH` | `/admin/llm-providers/{id}?force=` | `ManagedAICredentialUpdate` | `ManagedAICredentialReconcileResult` | Update parent + re-reconcile; `force` overrides Tier-2 block on removed members |
| `DELETE` | `/admin/llm-providers/{id}?force=` | — | `Message` | `409` with `blocked` list when any child is in use and `force` is absent |
| `POST` | `/admin/llm-providers/{id}/set-default` | — | `ManagedAICredentialPublic` | Sets every current member's child as their default; stamps `set_as_default=True` on parent |
| `POST` | `/admin/llm-providers/{id}/apply-to-existing?dry_run=` | — | `ManagedAICredentialApplyResult` | Add-only grant to every active account whose role is in `auto_provision_roles`, minus current members. `dry_run=true` writes nothing and emits no audit events |
| `POST` | `/admin/llm-providers/test-connection?managed_credential_id=` | `AICredentialTestRequest` | `AICredentialTestResult` | When `api_key` blank and `managed_credential_id` given, probes via stored parent key |

| `POST` | `/admin/llm-providers/{id}/members/{user_id}/retry` | — | `ManagedAICredentialPublic` | Requeue a terminally `failed` member. `400` if their provisioning has not failed; `404` if they are not a member. Emits `admin.ai_credential.mint_requested` carrying `retry_of: <last_error>` |

`POST /` and `PATCH /{id}` additionally answer `409` with the `auto_provision_conflict` body above when the request **newly** claims a `(role, mode)` default slot another record already owns. Both also answer `400` from `_validate_provisioning_shape` for an incoherent mode/key/provider combination, and `404` when a named provider admin credential does not exist.

### Provider admin credentials (`/api/v1/admin/provider-admin-credentials`)

**File:** `backend/app/api/routes/admin_provider_credentials.py` · **Auth gate:** `get_current_active_superuser` · **The secret is write-only** — accepted on create/update, in no response.

| Method | Path | Request | Response | Notes |
|--------|------|---------|----------|-------|
| `POST` | `/` | `ProviderAdminCredentialCreate` | `ProviderAdminCredentialPublic` | **Makes no provider call** — so a provider outage cannot stop an admin recording the configuration. Press Verify for that |
| `GET` | `/` | — | `list[ProviderAdminCredentialPublic]` | Bare array — see the envelope note below |
| `GET` | `/{credential_id}` | — | `ProviderAdminCredentialPublic` | `404` if not found |
| `PATCH` | `/{credential_id}` | `ProviderAdminCredentialUpdate` | `ProviderAdminCredentialPublic` | Omitting `secret` keeps the stored one |
| `DELETE` | `/{credential_id}?force=` | — | `Message` | `409 {code: "provider_admin_credential_in_use", message, minting_credential_count, live_minted_key_count}` while anything depends on it. `force` overrides, and the audit row records **the counts it overrode**, not merely that it was forced |
| `POST` | `/{credential_id}/verify` | — | `ProviderAdminCredentialVerifyResult` | Checks the secret **and** the enforcing spend cap in one press; stamps `last_verified_at` / `last_verify_error` |
| `POST` | `/{credential_id}/apply-spend-limit` | — | `ProviderAdminCredentialVerifyResult` | Setup action. Never applied after minting has started |

### Provider adapters (`/api/v1/admin/provider-adapters`)

| Method | Path | Response | Notes |
|--------|------|----------|-------|
| `GET` | `/` | `ProviderAdaptersPublic` (`{data, count}`) | Superuser-gated. Derived from `registry.all_adapters()`; `can_mint_now` additionally consults one `SELECT DISTINCT provider_type FROM provider_admin_credential`. Full field list in [provider_adapters_tech](provider_adapters_tech.md#get-apiv1adminprovider-adapters) |

### Owner-facing provisioning list (`/api/v1/ai-credentials/provisioning`)

| Method | Path | Response | Notes |
|--------|------|----------|-------|
| `GET` | `/ai-credentials/provisioning` | `list[UserKeyProvisioningPublic]` | `CurrentUser`; the caller's own keyless memberships (`pending`, `minting`, `failed`). **Declared above `/{credential_id}`** so the literal path is matched before the UUID route claims it |

> **List-envelope inconsistency.** `GET /admin/provider-adapters/` returns `{data, count}`; `GET /admin/provider-admin-credentials/` and `GET /ai-credentials/provisioning` return bare arrays (as does the pre-existing `GET /admin/llm-providers/`). Each is consumed correctly by the client that reads it, but "how does an admin list respond" now has two answers. Recorded in the business doc's [Known Gaps](admin_ai_credential_provisioning.md#known-gaps).

### Native Account-Config (`/api/v1/external/account-config`)

**File:** `backend/app/api/routes/external_account_config.py`
**Auth gate:** `CurrentUser` (standard JWT) + `client_kind in {"desktop", "mobile"}` (native gate)

| Method | Path | Auth gate | Response | Notes |
|--------|------|-----------|----------|-------|
| `GET` | `/external/account-config` | native JWT only | `AccountConfigResponse` | `403` for web JWTs; `401` for revoked desktop clients; `Cache-Control: no-store`; high-severity audit event |

**Status codes:**
- `200` — authenticated native client (providers list may be empty)
- `401` — unauthenticated, or revoked desktop/mobile client (via `get_current_user` revocation check)
- `403` — valid JWT but `client_kind` is absent or not in `{"desktop", "mobile"}`

---

## `ManagedAICredentialsService` (`managed_ai_credentials_service.py`)

Singleton: `managed_ai_credentials_service`

### Key public methods

| Method | Description |
|--------|-------------|
| `create(session, admin, data)` | Validate + encrypt canonical key; INSERT parent; call `reconcile(apply_fields=False, key_rotated=False)`. Returns `ManagedAICredentialReconcileResult`. |
| `update(session, admin, id, data, force)` | Apply scalar changes to parent (rotate `encrypted_data` if `api_key` supplied); call `reconcile(apply_fields=True, key_rotated=...)`. Omitting `target_user_ids` uses the current membership as desired. |
| `delete(session, admin, id, force)` | Reconcile to empty desired set (Tier-2 gated); if blocked and not `force`, abort (parent stays); else `DELETE` parent row. |
| `set_default_all(session, admin, id)` | `set_default` for every current member; stamp `parent.set_as_default=True`. Returns `ManagedAICredentialPublic`. |
| `list(session, admin, managed_by_id, target_user_id)` | Fleet-wide list, optional filters. |
| `get(session, admin, id)` | Single parent record; `404` if not found. |
| `resolve_test_key(session, id)` | Decrypt parent key for the Test Connection blank-api_key case. `404` if not found. |
| `add_members(session, *, parent, user_ids, actor)` | The Add pass on its own → `MemberAddition`. Idempotent (existing members are neither re-added nor reported). `actor` is **keyword-only with no default**: `None` means a system-initiated grant, and a route reaching it would be writing an unattributed grant, so "who did this" must be a decision at every call site. Does not affect what is written to the child — children are stamped with the parent's managing admin either way. Three callers: `reconcile`, `apply_to_existing`, `AccountProvisioningService`. |
| `apply_to_existing(session, admin, id, *, dry_run)` | → `ManagedAICredentialApplyResult`. Desired set = active users with `role ∈ auto_provision_roles`, minus current members. `dry_run` returns candidates and writes nothing. |

### `reconcile()` — the heart

```
reconcile(
    session, admin, parent, desired_user_ids,
    *, apply_fields, force, key_rotated, cleared_overrides=None
) -> ManagedAICredentialReconcileResult
```

- `desired_user_ids` — deduplicated; compared against `_current_members(parent)` (children keyed by `owner_id`)
- `apply_fields=False` — skip the Update pass (used on create, since there are no pre-existing members to update)
- `key_rotated=True` — forces key write-through in the Update pass even when other scalars are unchanged
- `force=True` — passes `force` to `delete_credential`; blocked members do not block the Remove pass
- `cleared_overrides` — `mode → the model override this request dropped`. Only `update()` can know it: once the parent row is written, a stored `None` cannot say whether it was never set or has just been retracted, so the *request* carries the transition down
- The Add pass is **delegated to `add_members`**, not duplicated, so a change to how a member is created cannot land in one of three places
- The Remove pass captures `_modes_pointing_at(owner, child.id)` **before** the delete and calls `_release_model_overrides` after it — afterwards the pointer is already NULL (`ondelete="SET NULL"`) and which slots the child owned is unrecoverable

### Session repair on per-member failures (`add_members`, both reconcile passes, `_add_child`)

Four handlers in this service catch a per-member failure, record it, and carry on. Every one of them calls `restore_session(session)` (`backend/app/utils.py`) **first**, then logs from pre-snapshotted locals, then appends its entry:

| Handler | Records | Why the rollback is required |
|---------|---------|------------------------------|
| `add_members` per-user `except` | `skipped(reason="provision_failed")` | A statement-level failure leaves the transaction aborted; the log call is itself a query, so a handler that logs first throws from inside itself and the exception escapes every net written to catch it |
| reconcile **Remove** pass `except` (after the `AICredentialInUseError` arm) | `blocked(reason="remove_failed")` | `reconcile` ends at `_to_public`, which queries. Recording a block and continuing on an aborted transaction turned a per-member problem into a `500` out of `PATCH /admin/llm-providers/{id}` |
| reconcile **Update** pass `except` | `skipped(reason="update_failed")` | Same |
| `_add_child` post-commit wiring `except` | Nothing — the member is **retained** | `_stamp_child` has already committed the child. Without the rollback the exception escaped into `add_members`, which reported the user `provision_failed` while their child row sat committed and they were a member by every definition this service uses — and on the auto-provision path wrote an `auto_provision_failed` security event into the feed of someone who *had* received the credential |

`HTTPException` is re-raised in each of these, unrolled-back: type-validation errors are the caller's problem, not a per-member skip.

Rolling back is safe in all four: the child is committed by `_stamp_child`, and `set_default`, `_apply_sdk_defaults`, `delete_credential`, `_release_model_overrides` and `_update_child_fields` each commit their own work, so nothing pending belongs to anyone but the failed attempt.

### `_update_child_fields()` — Update pass detail

Diffs parent scalars against the child (and the child's decrypted data). Only writes when something changed (idempotency). Returns `True` iff the child was mutated.

Clear-through limitation: `update_credential` treats `None` as "leave unchanged", so `base_url`/`model` cannot be cleared back to `None` (for `openai_compatible` both are required anyway). `expiry_notification_date` is cleared directly on the child row when the parent value is `None`.

Admin-curated model metadata write-through: `default_model` and `available_models` are written directly on the child row (bypassing `update_credential` — they are non-secret plain columns, not part of `AICredentialData`). Idempotent: the child is only mutated and flagged as `updated` when the stored values differ from the parent's. `available_models` differentiates `None` (no change) from `[]` (explicit clear) by comparing the exact stored list values — the parent itself carries `None` vs `[]` as the source of truth.

Default flag logic:
- `set_as_default=True` and `child.is_default=False` → `set_default(child, owner)`
- `set_as_default=False` and `child.is_default=True` → `_clear_child_default(child)` (clears profile blob + SDK-default pointers that reference this child)

### `_stamp_child()` — curated model metadata write-through on create

After `create_credential` creates the child, `_stamp_child` also writes `default_model` and `available_models` from the parent directly onto the child row (same pattern as the structural markers). This ensures newly-added members inherit the current curation immediately without a separate reconcile.

### Auto-provision uniqueness (`_claimed_slots` / `_validate_auto_provision_uniqueness`)

`_claimed_slots(roles, modes, set_user_sdk_defaults) -> set[tuple[str, str]]` — the `(role, mode)` default slots a configuration lays claim to. Empty unless the record does all three things: auto-provisions to a role, wires SDK defaults, and claims a mode. Modes outside `{"conversation", "building"}` are filtered out, because a mode that wires nothing cannot be fought over (which is also the only reason an unvalidated `sdk_default_modes` typo does not produce a phantom collision).

```
_validate_auto_provision_uniqueness(
    session, parent_id, roles, modes, set_user_sdk_defaults,
    *, previously_claimed=None
) -> None                      # raises ManagedCredentialConflictError
```

- **Scoped to the transition, not the state.** Only `claimed - previously_claimed` can raise. Renaming a record already sitting in a conflicting configuration must not 409 on a collision the admin did not introduce.
- Validating the effective *end state* reads as safer and is not: it makes a stale absolute payload indistinguishable from a deliberate edit, so a client rebuilding its request from an open-time snapshot gets refused for someone else's change — and a validator taught to tolerate that would also have masked the membership clobber `target_user_ids` had.
- `parent_id` is the record being written (excluded from the search), or `None` on create, where `previously_claimed` is empty and every slot is new.
- `create()` validates **before** the row is inserted, so a conflict never leaves a half-configured parent behind.
- `update()` reads `previously_claimed` and `previous_overrides` off the parent **before writing a single field** — both are transitions, and neither is recoverable once the new values are in. It then validates against the *effective* values (submitted where given, stored otherwise), so a PATCH that introduces a conflict cannot half-apply.

### Model-override write paths

Three functions write `User.default_model_override_<mode>`, and the differences between them are the contract:

| Function | Runs when | Behaviour |
|---|---|---|
| `_apply_sdk_defaults` | A member is **added** (from `_add_child` only) — the credential pointer is moving onto a different credential | Writes the override **unconditionally** for every mode the parent claims, including back to `NULL` when the parent has none. A reset, not a wipe: whatever sat in the slot described another credential and may name a model this provider does not serve |
| `_sync_model_overrides` | **Every** update against a slot the child already occupies | Writes only while the pointer still names this child, and only when the parent has an opinion. A stored `None` means "no opinion", not "clear theirs". `cleared_overrides[mode]` is the third case: retract the member's pin, but **only when it still equals the dropped value** — a member who picked their own model keeps it. Returns `True` iff the owner row was written |
| `_clear_child_default` / `_release_model_overrides` | The child's default is cleared, or the child is deleted | Tear the override down with the pointer it belongs to. `_clear_child_default` clears pointer + `default_sdk_<mode>` + override; `_release_model_overrides` (the reconcile Remove path, and therefore `DELETE /{id}`) clears the override only — the two knowingly disagree about `default_sdk_<mode>`, see Known Gaps |

Asymmetry worth restating: *setting* an override overwrites a model the member picked; *clearing* one only retracts this record's own value. So a member's own pick, once overwritten by a set, is not restored by the later clear. A mode dropped from `sdk_default_modes` is not visited at all — neither its override nor its pointer is torn down.

Shared constants: `_MODE_POINTER_ATTR` and `_MODE_OVERRIDE_ATTR` map `mode → User` attribute name; membership of these dicts is also the validity test for a mode string coming off the JSON column. `_modes_pointing_at(owner, child_id)` is the one place "does this slot belong to this child" is answered.

### `_count_default_overwrites(session, parent, candidates)`

Backs `defaults_overwrite_count` so the confirm dialog can say what the action costs, not only what it gives. Counts a candidate **once** however many things they lose — the admin's question is "how many people does this disturb", not "how many columns move".

It must look at **both** of the axes `_add_child` writes, because they are independent flags applied independently:

| Axis | What is counted | Why |
|------|-----------------|-----|
| `set_user_sdk_defaults` | For each claimed mode, `default_ai_credential_<mode>_id` **or** `default_model_override_<mode>` is non-NULL | `_apply_sdk_defaults` resets the slot wholesale on a claim, so a member with no pointer but a model they picked for that mode still loses something. Skipped entirely when no provider adapter serves the parent's type (`_sdk_engine_for` returns `None`) — `_apply_sdk_defaults` returns immediately for such a type, so counting it would promise a change that will not happen |
| `set_as_default` | The candidate owns an `AICredential` of the parent's type with `is_default=True` | `ai_credentials_service.set_default` unsets whatever the owner's current default of that type is and rewrites the legacy per-type profile blob. Mirrors that service's own unset query |

The `set_as_default` axis is resolved by a **single `in_()` query** over the whole candidate list, not one query per candidate — this runs inside a dry run the admin is waiting on. The SDK axis is evaluated from the already-loaded `User` rows.

Returns `0` only when the record wires no defaults at all — the genuinely harmless configuration, and the only one the dialog is entitled to describe as free.

**The bug this shape exists to prevent.** The first version opened with `if not parent.set_user_sdk_defaults: return 0`. A record whose only default-writing flag was `set_as_default` therefore previewed as `candidate_count=N, defaults_overwrite_count=0`, the dialog's cost paragraph was gated on the same flag and said nothing at all, and confirming it silently stripped every candidate's own default credential.

**Not counted, on purpose: `default_sdk_<mode>`.** The engine string is never NULL, so including it would make the count equal `candidate_count` for every record that claims a mode. This is why the zero-copy in the dialog is worded as "nobody has picked a credential or a model for these slots yet" rather than the broader "no existing choice is replaced" — an OpenAI record claiming the conversation slot does move everyone's engine, and the broader sentence would be an overclaim. Do not widen it.

**Distinct from the `(role, mode)` uniqueness rule.** `_claimed_slots` / `_validate_auto_provision_uniqueness` still ignore `set_as_default` (Known Gap 5, accepted debt): two records may both claim to be their members' default-for-type and neither `409`s. That the *preview* counts the axis does not change what the *validator* refuses. The two questions are separate — the admin is told what the second record costs, and then allowed to confirm it — and collapsing them into one is the same reasoning error the counter's original bug was made of.

### `_normalize_auto_provision_roles(value)`

`None` passes through as "no change". Otherwise trimmed, de-duplicated order-preservingly, and checked against `VALID_AUTO_PROVISION_ROLES` — an unknown role raises `400` rather than being silently dropped, because a mistyped role would otherwise save successfully and then do nothing at the next signup with no clue why. There is deliberately **no** counterpart for `sdk_default_modes`.

### `_sdk_engine_for(cred_type)`

One registry lookup returning `registry.get_adapter(cred_type).sdk_engine`, or `None` for a type no adapter serves.

| `AICredentialType` | `adapter.sdk_engine` |
|-------------------|---------------------|
| `ANTHROPIC` | `"claude-code/anthropic"` |
| `MINIMAX` | `"claude-code/minimax"` |
| `OPENAI` | `"opencode/openai"` |
| `GOOGLE` | `"opencode/google"` |
| `OPENAI_COMPATIBLE` | `"opencode/openai_compatible"` |

The values above are **declared on the adapters**, one per module under `backend/app/services/ai_providers/`, not in this service. They used to be a five-entry `_TYPE_TO_SDK_ENGINE` dict here and a byte-identical copy in a second module, with a third encoding of the same mapping (split into `(engine, provider)` tuples) in `external_account_config_service`. All three are gone; `catalog_engine_provider` is derived from `sdk_engine` by splitting on `/`. A `tests/architecture/provider_adapter_registry_test.py` check forbids re-declaring a dict keyed on `AICredentialType` outside the adapter package.

---

---

## `AccountProvisioningService` (`services/users/account_provisioning_service.py`)

Static-method service. `on_account_created` is called by `UserService.create_account` after the account row is committed; `provision_explicit` is called by `InvitationService.invite`, after the same chokepoint has committed the row with `skip_auto_provision=True`.

| Method | Description |
|--------|-------------|
| `on_account_created(session, user, origin) -> ProvisioningReport` | Grants every managed credential whose `auto_provision_roles` contains `user.role`. **Never raises**, unconditionally — including when handed a session whose transaction is already aborted. Returns a value, never an error; its production caller ignores it, which is what lets tests assert on failures without the production path branching on one |
| `provision_explicit(session, user, origin, *, managed_credential_ids, actor) -> ProvisioningReport` | The invitation wizard's entry point, and the only sanctioned way an explicit list is granted at account creation. Same net, same body, same report as the automatic path — `_guarded` and `_provision` are shared, so the two cannot drift. `managed_credential_ids=None` selects the automatic set by the same predicate; `[]` grants nothing. `actor` is the acting superuser and is **required** (keyword-only, no default), and reaches both `add_members` and `details.actor` |
| `_guarded(session, user, origin, run, *, label)` | The outer never-fail net, factored out so the two entry points cannot each forget `_restore_session` or log `user.id` before the repair. `label` (`"automatic provisioning"` / the invite path's own) is required and keyword-only for the same reason `add_members`' `actor` is: with a default, the invite path would silently report itself as automatic provisioning and an admin debugging a failed invitation would grep for a string that is never written |
| `_provision(session, user, origin, *, parent_ids, actor, label)` | The shared body. `parent_ids is None` selects the automatic set; a list selects exactly those records, deduplicated, with any id that no longer names a record reported as a `managed_credential_not_found` skip rather than dropped. Everything after the selection — the per-parent guard, the skip reporting, the audit events — is identical for both entry points. The inactive short-circuit runs *before* the selection and is the one place they differ: it reports one `user_inactive` skip per **requested** credential, which only the explicit path can name |
| `on_account_deactivated(session, user) -> None` | Deletes the user's minted child credentials, moves their memberships to `suspended`, and hands the provider revokes to the background loop. **Shared credentials are untouched** — one key held by many people must not be destroyed because one holder left. Synchronous like both of its callers: the database work happens inline (so nothing downstream can observe an account that is deactivated but still holds a live key row) and only the provider call is deferred. Wrapped in its own never-fail net — deactivating an account must not fail because a provider record could not be tidied |
| `on_account_reactivated(session, user) -> None` | The mirror. Moves `suspended` memberships back to `pending` at zero attempts; a **fresh** key is minted. It exists because the membership survived the deactivation — without it a reactivated employee would be a member with a `suspended` row nothing ever picks up |
| `on_account_deleted(session, user, *, actor_id) -> list[RevocationRequest]` | **Call before the delete, schedule after it.** Both deletion routes are a bare `session.delete(user)`, so the membership rows and their provider handles are gone the moment it commits. Returns the requests rather than scheduling them, so the provider is only contacted if the deletion actually commits. `actor_id` names whose feed the revoke is recorded in — deliberately *not* the key holder, whose `user` row is about to be gone; `None` for self-deletion, where subject and actor are the same and both are being removed |

**The `is_active` call sites are two**, and the list is worth keeping accurate because the last audit of it found one missing: `api/routes/users.py::update_user` (on the transition) and `InvitationService._apply_reinvite_updates` (the re-invite path — it flips `is_active` on an *existing* account and is the caller a reader would not think to look for). **Deletion is explicitly not a third:** `delete_user` / `delete_user_me` call `on_account_deleted` instead, because a deleted account's rows are gone by the time anything could read them.

Result dataclasses: `ProvisioningReport(added, skipped)`, `ProvisionedCredential(managed_credential_id, child_credential_id)`, `ProvisioningSkip(managed_credential_id, reason)`. `reason` is a stable machine string — the reconcile skip reasons (`user_not_found`, `user_inactive`, `provision_failed`), `add_members_failed` when the call itself raised, or `managed_credential_not_found`, which only the explicit-list path can produce and is therefore the one most likely to be missing from a hand-written frontend map.

The invite route projects this report into its own narrow `InviteProvisioningSummary(added_count, skipped: list[InviteProvisioningSkip], provisioning_failed)` rather than returning `ManagedAICredentialReconcileResult`: that shape carries `removed`/`blocked`/`updated`, which an add-only grant can never populate, and a `record` projection whose construction costs a per-member user lookup and a key decrypt.

`provisioning_failed` carries `ProvisioningReport.failed`, which `_guarded` sets when the outer net fires. Without it a total failure — a raise in `_provision`'s prologue, before any credential has been selected — returns a report that is byte-identical to a deliberate grant of nothing (empty `added`, empty `skipped`), and the wizard renders both as "No AI credentials were granted". It is deliberately not a skip entry: a skip names a credential, and this failure can happen before any credential has been named. The two "nothing to do" returns inside `_provision` (an inactive account, an empty explicit id list) stay `failed=False` — they are outcomes, not failures. The inactive one is nevertheless not *empty*: it returns one `user_inactive` skip per requested credential, for the same reason `failed` exists, since an admin who ticked three credentials and invited a deactivated account would otherwise see exactly the screen of an admin who ticked none. That state is now reachable — re-inviting an account an administrator deliberately deactivated no longer reactivates it (`InviteUserRequest.is_active` is `bool | None`, and `None` means the submission did not state one). No security event is written for these skips: a medium-severity row per credential, in the feed of an account that has done nothing, is noise about an outcome that was never in doubt. The wizard renders them as one amber line, not N identical skip lines.

**Two error-handling rules, both non-obvious enough to state:**

- **Repair the session before touching it.** A failed attempt can leave the transaction aborted, and in that state *every* session operation raises — including the lazy attribute load behind an innocent-looking `logger.warning("… %s", user.id)`. So identifiers are snapshotted into locals while the session is known good, `_restore_session` runs first, and only then does anything get logged. The first parent in the loop hides this (`user` is still fresh from `create_account`'s refresh); it is the second, after a child credential has committed and expired everything, that bites.
- **Roll back unconditionally.** `AccountProvisioningService._restore_session` is a thin wrapper over the shared `restore_session(session)` in `backend/app/utils.py` (shared with `ManagedAICredentialsService`, the other end of this same path); it deliberately does **not** guard on `session.is_active`. That predicate detects only half the problem: a *flush* failure deactivates the `SessionTransaction`, but a *statement* failure (a `select` that errors, a lock timeout, a serialization failure, a dropped connection) leaves Postgres' transaction aborted while `is_active` stays `True` — so the guard would skip exactly the case that needs the rollback, and the caller's next commit (`register_user` commits again to send its confirmation email) dies with "current transaction is aborted". The rollback is safe by construction: the account row is committed before the call and `add_members` commits each child individually, so anything still pending belongs to the failed attempt. The helper lives in `app.utils` rather than on either service precisely because it is a pure session-lifecycle concern with no domain knowledge, and two copies is how one of them ends up guarded on `is_active` again. Whether rolling back is *safe* is left to each caller — `restore_session` never decides that, and never raises.

Other structural choices: the outer `try` in `on_account_created` wraps the prologue (deferred import, parents query, role filter) that the per-parent guards do not cover; `user_id` is pre-bound to `None` and snapshotted *inside* the `try` because reading `user.id` is itself a session operation. Parents are filtered in Python (the table holds a handful of rows, and a portable JSON-containment predicate over a `json` — not `jsonb` — column is more machinery than the saving is worth), with an `isinstance(…, list)` guard so a hand-edited non-list row cannot raise on the signup path. Inactive accounts return an empty report before any parent is looked at — no children, no skips, no medium-severity events in the feed of an account nobody can sign into.

Security events are constructed and committed **directly** here rather than through `SecurityEventService.create_event`: that method is `async` (its body awaits nothing, but the signature is), and this path is synchronous and called from inside both sync and async routes, so there is no loop to schedule it on. `_emit` is best-effort and never raises — an audit row that cannot be written must not break an account creation the caller was told could not fail.

---

## `KeyProvisioningService` (`services/credentials/key_provisioning_service.py`)

Singleton: `key_provisioning_service`. **Owns exactly one thing: the provisioning lifecycle of a membership row.** It never creates or deletes a membership — that is `ManagedAICredentialsService`'s, because "who is a member" and "does that member have a key yet" are two questions and giving them one owner is how they get answered inconsistently.

### Constants

| Name | Value | Why |
|------|-------|-----|
| `MAX_ATTEMPTS` | `5` | Bounded on purpose — the point of the ceiling is that somebody eventually has to look |
| `BACKOFF_SECONDS` | `(60, 300, 900, 3600)` | The last value repeats if the list runs short, but it never does; the ceiling is the real stop |
| `BATCH_SIZE` | `25` | One pass is a provider round trip per member, so an unbounded batch is a tick that never ends |
| `KEYLESS_STATUSES` | `(pending, minting, failed)` | The one list used by **both** owner-facing readers, so "is a key on the way" and "what should the dashboard show" can never answer differently |
| `IN_FLIGHT_STATUSES` | `(pending, minting)` | `failed` is terminal and is *not* in flight — a screen that treated it as such would spin forever |

### Methods

| Method | Notes |
|--------|-------|
| `converge(session, *, limit=None) -> ConvergeReport` | Attempts every due membership. **Takes a session and is directly awaitable** — that is the whole test surface, since the scheduler never runs under pytest. Ordering is `next_attempt_at ASC NULLS FIRST`, spelled explicitly: Postgres sorts NULLs last on ASC and a NULL here means "never attempted", so the default would queue every brand-new member behind every backed-off retry. Per-member exceptions are caught, the session **repaired before logging** (a statement-level failure leaves the transaction aborted, and every ORM attribute in a log line is a query), and the pass continues |
| `_attempt(session, membership, report)` | One mint. Settles a shared parent's row to `not_applicable` and an inactive owner's to `suspended` before doing anything else; then **claims the row (`minting`, attempts+1, commit) *before* the configuration checks** — every path from there is an attempt and must cost one. Revokes any stale `external_key_ref` first, mints, commits the handles in their own commit, materialises the child, settles to `provisioned` |
| `_settle(...)` / `_record_failure(...)` | Terminal write, or back off — and go terminal-`failed` at the ceiling. Without the ceiling a permanently broken configuration would sit at `pending` with an ever-later retry, which reads as "still working" to every surface that shows it |
| `revoke_now(session, requests) -> int` | Destroy the named keys. Directly awaitable with a session, for the same reason `converge` is |
| `schedule_revocations(requests)` | Fire-and-forget wrapper. The coroutine opens **its own session**, because the caller's may be committed, rolled back or closed long before it runs |
| `collect_user_revocations(session, user_id, *, audit_user_id)` | Every minted key this user holds, as `RevocationRequest`s. Read **before** the rows are deleted |
| `suspend_user_memberships(session, user_id)` | Deactivation. **Delete the child first, revoke second**; a blocked child delete emits `revoke_blocked` and leaves the key live. Which rows have a key to revoke is `holds_provider_key(membership)` — **not** a status list. It previously skipped `failed` memberships on the premise that a terminal failure holds no key; two paths reach the ceiling with a live `external_key_ref`, so the key survived the deactivation. A terminal failure now keeps its `status`, `last_error` and `provision_attempts` while its key is revoked |
| `resume_user_memberships(session, user_id) -> int` | Reactivation: `suspended` → `pending` at zero attempts |
| `requeue_failed_member(session, *, parent_id, user_id)` | The way out of `failed`. `404` if not a member, `400` if not failed. **Deliberately not folded into `add_members`** — re-adding an existing member is a no-op there, and making it a requeue would mean any PATCH that merely renames the record quietly resets every durable failure it touches (reconcile passes the current membership list as the desired one). `last_error` is deliberately kept |
| `list_user_provisionings(session, user_id)` | The owner-facing list. `suspended` is deliberately absent — it only exists on a deactivated account, and a deactivated account has nobody looking at that screen |
| `api_key_onboarding_states(session, user_ids) -> dict[UUID, AIKeyOnboardingState]` | The whole onboarding answer, taken here and **provider-agnostic on both halves**. `has_key` = `ai_credentials_service.owner_ids_with_a_default(session, ids)`; else `preparing` when a membership row is in `IN_FLIGHT_STATUSES`; else `needs_key`. Batched because two callers ask for a page of members at a time |
| `api_key_onboarding_state(session, user_id)` | The single-user wrapper — `api_key_onboarding_states(...).get(user_id, NEEDS_KEY)`. There is one predicate, not two |

### `RevocationRequest.audit_user_id`

**Required, keyword-only, no default** — for the same reason `add_members`' `actor` is: "there is nobody to tell" must be a decision at every construction site. It was a defaulted `None` for one commit, and in that commit the busiest revocation path of all — an admin removing a member — inherited the escape hatch meant for self-deletion and wrote no audit row at all.

`security_event.user_id` is NOT NULL with an FK to `user`, so on the account-deletion path writing to the holder's feed is not merely pointless — it is an integrity error that would take the whole revoke loop down with it. `_audit_revocation` is best-effort and repairs the session before logging.

### The claim token — every post-*claim* write is conditional

Committing the handles before the key protects a *crash*. It does not protect a **lost claim**: `_attempt` claims the row, then awaits the provider, and in that window the row can be settled by somebody else — an admin deactivating the account, deleting it, or force-deleting the parent record. When the mint returns, the row it was minting for may no longer be the row it claimed.

The governing invariant is a comment at the mint site:

> *A minted key must never end up live-but-unrecorded. If you cannot store it, revoke it; if the revoke fails, emit the durable event with the external ref in it.*

A key held in a local variable and named by nothing else is the one state from which no later pass — no retry, no deactivation, no deletion sweep — can ever recover, because nothing knows it exists.

**The mechanism.** `_MintClaim` is a `NamedTuple` of `(membership_id, user_id, parent_id, stamp)`, taken when the row is claimed. `_write_under_claim` issues a conditional `UPDATE … WHERE id = :id AND status = 'minting' AND updated_at = :stamp … RETURNING updated_at`. A zero-row result means the claim is lost; a one-row result returns `claim._replace(stamp=<the stored stamp>)`, so a chain of writes each carries the stamp the *database* recorded rather than the one this process intended.

**Every write an attempt makes after taking the claim goes through it — the success path and the failure paths alike.** The failure path is conditional for a reason of its own, and it is not symmetry: `_record_failure` writes `pending` with a retry time, so an unconditional version would resurrect whatever the row became while this process was inside the provider call. The concrete case is a deactivation landing inside the mint `await`, settling the row `suspended` — a status whose whole meaning is "this account is disabled and nothing should be minted for it" — and an unconditional failure write then putting it back to `pending`. (`_settle` is for the statuses written *outside* an attempt, before the claim is taken; nothing an attempt writes goes through it.)

| Step | On a lost claim |
|---|---|
| Clear a stale `external_key_ref` after the pre-mint idempotency revoke | `report.skipped.append("claim_lost")`, return |
| Store the new `external_key_ref` (its own commit, before the key) | `_discard_orphan_key(...)`, `claim_lost`, return |
| Materialise the child credential | The provider call is over, so the write itself is the child insert; a raise here is `restore_session` + `_record_failure(..., "child_create_failed")`, which is **itself claim-conditional** and returns `False` on a lost claim → `claim_lost`. Nothing is unnamed in that branch: the handles were committed under the claim before it, so whoever took the row can see the key and destroy it |
| Settle to `provisioned` with the child id | `_discard_orphan_child(...)` **and** `_discard_orphan_key(...)`, `claim_lost`, return |

**`_discard_orphan_key` is the invariant in code.** It builds a `RevocationRequest` (with `audit_user_id = claim.user_id` only when `_user_exists` — a `select(User.id)`, not a `session.get`, because the row may be gone), calls `provisioner.revoke`, and emits `admin.ai_credential.revoked`. If the revoke itself raises, it repairs the session and emits **`admin.ai_credential.revoke_failed` (severity `high`) carrying the external ref** — that event is then the only record of the key.

Three races are covered by tests: **deactivation**, **deletion**, and **force-delete of the parent record**, each run against an in-flight mint.

### Crash safety

Provider handles are committed **before** the key is stored, so a process that dies mid-mint leaves a row that names the orphan it created; the next attempt revokes that first. A crash therefore costs a wasted key rather than a leaked one.

**One window remains**, stated rather than papered over: between the provider creating the service account and this process committing anything there is no record. The create is a single call that returns the secret — splitting it would mean passing `create_service_account_only`, which makes the response carry no secret at all — so a crash inside that window leaks one service account. It is visible in the provider's own console.

### Scheduler (`key_provisioning_scheduler.py`)

APScheduler `BackgroundScheduler`, 1-minute `interval`, `max_instances=1`, `coalesce=True`. Started in `main.py`'s lifespan alongside the platform's other schedulers, behind the same `not settings.TESTING` gate — which is why `converge` takes a session rather than opening one: it is the only test surface.

- **Leader lock** via `app.core.db.leader_session(KEY_PROVISIONING_LOCK_KEY)`, not re-derived here. `pg_try_advisory_lock` is *connection*-scoped while an engine-bound `Session` returns its connection to the pool at every `commit()`, and this sweep commits per member — an inline copy would strand the lock on a pooled connection and lock every later tick out permanently. Three schedulers had that code and one of them had that bug; the helper is now the only implementation.
- `KEY_PROVISIONING_LOCK_KEY = 0x4B45594D494E54` ("KEYMINT"), **its own key**: two workers minting for the same membership would create two provider keys and remember one.
- **Main-loop bridge.** The APScheduler job runs on a worker thread and submits the sweep via `asyncio.run_coroutine_threadsafe` onto the loop captured at startup, because creating a child credential emits events whose handlers are `asyncio` tasks on the *currently running* loop — under `asyncio.run()` they would land on a throwaway loop that closes the moment the sweep returns. Same idiom as the status-repair sweep.
- `SWEEP_WAIT_TIMEOUT_SECONDS = 180`, a constant rather than something derived from the tick interval: shutdown waits on this thread, so a configurable wait would let a long interval turn a deploy into a long hang.

---

## The dashboard wall: latch, banner and polling

`routes/_layout/index.tsx`. Three things are load-bearing.

**1. The full-page wall is a first-render decision.** `wallAllowedRef` is a `useRef<boolean | null>(null)`, set once on the first render where `credentialsStatus !== undefined` to `credentialsStatus.api_key_onboarding_state === "needs_key"`. `showKeyWall` requires `wallAllowedRef.current === true` **and** the current state still being `needs_key` **and** no local skip.

**The latch condition is `credentialsStatus !== undefined`, and nothing else.** It was `!credentialsLoading`, which is a different question: React Query's `isLoading` is `isPending && isFetching`, so it is `false` in three states that are not an answer, and the latch was then wrong in both directions. Latching `false` — a `has_key` served from a five-minute cache on a fresh page entry, while the refetch that says `needs_key` is still in flight — leaves somebody who needs a key with only the banner, on exactly the entry where there is no draft to protect and the wall is what should show. Latching `true` — offline, where `fetchStatus` is `"paused"` and `isLoading` is therefore `false` — walls a person who *has* a key, behind a "Skip for now" that writes a permanent `localStorage` flag, with a paused query that never resolves to undo it.

Without the latch the state polls, so a person who was working — mid-draft, files attached, an agent selected — could have the entire page replaced by the paste-a-key screen ten seconds later and lose all of it. That is a data-loss bug regardless of whether the flip was computed correctly, and correctness does not make it rare enough to leave in: an admin deleting a credential, or a minted key being revoked, both flip it.

**2. Post-load, the same information is a non-destructive inline banner.** `showKeyBanner = !showKeyWall && onboardingState !== undefined && onboardingState !== "has_key"`, rendered at the page top beside `EnableTwoFactorBanner`.

Two guards to note. It is **not** suppressed by `onboardingSkipped`: "Skip for now" is a permanent flag that dismisses the *wall* — the thing that takes the page away — and a nudge one click silences forever is not a nudge, least of all for the account that cannot run an agent. It **is** suppressed while `onboardingState` is `undefined`, because a banner about a state nobody has stated is a guess. Two copies:

- `preparing` — **"Your AI access is being set up"** / "An administrator is creating an API key for your account. This page updates on its own when it is ready — there is nothing for you to do." `preparing` never gets the full-page wall at all, and it previously rendered *nothing whatsoever*, so that person saw a dashboard where nothing worked and no explanation of why.
- `needs_key` after load — **"No AI credential yet"** / "Agents need an API key before they can run. Add one in Settings, or make an existing credential your default." (The second clause is the one that matters: the state is about a *default*, not about owning a credential.)

**3. Both non-`has_key` states poll.** `refetchInterval` is keyed off the server's own answer — `query.state.data?.api_key_onboarding_state === "has_key" ? false : 10_000` — rather than a second predicate in the browser. `needs_key` used not to poll, which is why the invite happy path needed a manual reload to leave the wall: an administrator adding a key by hand changes this person's state without them doing anything, exactly as a landing mint does.

The wall is deliberately **not** computed from `has_anthropic_api_key` plus a browser-side membership query. That would make this file a second implementation of a policy the server owns, and the two would answer differently the first time either side changed.

#### Why `api_key_onboarding_state` is required with no default

`UserPublicWithAICredentials.api_key_onboarding_state` carries no default, and the reason is the closing lesson of the phase rather than a style preference.

A default on the model makes the field **optional in the generated client** (`types.gen.ts` / `schemas.gen.ts` drop it from `required`). Every browser reader then has to write `?? "needs_key"` — and that fallback *is* the client-side policy `AIKeyOnboardingState`'s own docstring exists to forbid. It is also silently wrong: the browser cannot distinguish *"the server has not answered yet"* from *"the server said `needs_key`"*, so a query that is pending, paused (offline) or errored becomes a confident `needs_key`.

This was the one field a **full-page wall** depended on. The cost of the wrong direction is a person who has a key losing their whole dashboard to a paste-a-key form, behind a "Skip for now" that writes a permanent `localStorage` flag. Required makes `undefined` mean exactly one thing, and both readers act on that: the wall latch takes `credentialsStatus !== undefined` as its condition, and the banner renders nothing while the state is `undefined`.

`ManagedAICredentialMember.api_key_onboarding_state` is required for the same reason on the admin side — `_member_dto` takes `key_state` as a required keyword, computed in batch by `_owner_key_states`.

---

## `ProviderAdminCredentialsService` (`services/credentials/provider_admin_credentials_service.py`)

Singleton: `provider_admin_credentials_service`. Superuser-only CRUD plus verification. **The secret leaves this module only as an argument to a provider call.**

| Method | Notes |
|--------|-------|
| `decrypt_secret(record)` | The administration secret in plaintext, for a provider call |
| `usage_counts(session, credential_id) -> (int, int)` | `(managed records minting through it, live keys only it can revoke)`. The live count is `external_key_ref IS NOT NULL` — **not** a status list: a membership mid-mint holds a key the provider has already created, and a list naming only `provisioned` would undercount exactly the rows whose key is hardest to find again. The ref is the key's existence; the status is where the row is in its lifecycle |
| `create` / `list` / `get` / `update` | `update` keeps the stored secret when `secret` is omitted |
| `delete(session, admin, id, *, force=False) -> (int, int)` | Raises `ProviderAdminCredentialInUseError` unless `force`. Returns the counts **as they were at the moment of deletion**, so a forced disconnect is audited with what it overrode — `"forced: true"` on its own does not say how many |
| `verify(session, admin, id)` | Two provider calls (`verify_admin_access`, `verify_spend_limit`), one result. The cap is **read**, not applied: applying one is a setup action and must be a decision, not a side effect of pressing Verify. A non-enforcing limit returns `ok=False, error="project_not_capped"` |
| `apply_spend_limit(session, admin, id)` | Setup only |
| `to_public(...)` | Fills `has_secret`, the two counts and `delete_blocked` |

---

## Admin-Curated Model Normalization (`_normalize_default_model` / `_normalize_available_models`)

Both helpers live on `ManagedAICredentialsService` and are called at `create()` and `update()` time before the values are stored on the parent.

**`_normalize_default_model(value)`**
- Strips any `provider/` prefix via `_strip_provider_prefix` from `model_catalog.py`.
- Trims whitespace; caps at 255 characters.
- Blank or all-whitespace input returns `None`.

**`_normalize_available_models(value)`**
- `None` input returns `None` unchanged (distinguishes "no change" from an explicit empty list).
- A list is processed entry-by-entry: strip `provider/` prefix, trim, drop blanks and duplicates (order-preserving), cap each entry at 255 characters.
- Caps the output list at 100 entries.
- An all-blank input list returns `[]` (explicit clear).

These caps bound payload size and prevent prefix-collision bugs in the OpenCode config builder.

---

## SDK / Environment Resolution (`environment_lifecycle.py`, `sdk_constants.py`)

### Per-mode credential-default bag carriers

`make_empty_credential_bag()` (in `sdk_constants.py`) includes two extra keys:

```python
"model_default_conversation": None,
"model_default_building": None,
```

These are filled during `_update_environment_config` in `environment_lifecycle.py` by `_set_mode_default_model_in_bag(bag, mode, cred_row)`, which reads `cred_row.default_model` and writes it to the appropriate bag key when non-empty.

**Resolution sites:** `_resolve_assigned_credential_into_bag` (for credentials explicitly linked to the environment's `conversation_ai_credential_id` / `building_ai_credential_id`) and `_fallback_fill_bag_for_sdk` (for type-level-default fallbacks). Both sites call `_set_mode_default_model_in_bag` after resolving the credential row, so every code path that fills the bag also carries the credential's `default_model`.

**Important note (create vs reconfigure):** `environment_service.create_environment` builds its bag for API-key extraction only; it does NOT populate the `model_default_*` carriers. The credential `default_model` is re-resolved inside `_update_environment_config` (which all lifecycle paths — create, reconfigure, rebuild — go through), so the net effect is correct.

### Override injection (`_generate_env_file`, `_generate_opencode_config_files`)

Both file-generation methods receive `model_default_conversation` and `model_default_building` as parameters. For each mode, the inner `_resolve_mode_model` function applies:

```python
credential_default = model_default_building if mode == "building" else model_default_conversation
override = env_override or credential_default
return resolve_model(engine, provider, mode, override, ...)
```

This means:
- If the environment has an explicit `model_override_building` / `model_override_conversation`, that wins.
- If not, the credential's `default_model` is injected as the override into `resolve_model`.
- `resolve_model`'s contract is unchanged; the credential default is just a source for the `override` argument.

**For Claude Code:** the resolved model is written to `MODEL_BUILDING` / `MODEL_CONVERSATION` env vars (picked up by the `claude_code_sdk_adapter` as `options.model`).

**For OpenCode:** the resolved model is written to the `opencode.json` `model` field in `_build_config`.

---

## Model Health Consistency (`model_health_service.py`)

`_evaluate_mode` recomputes the effective model to classify health. It now mirrors the same precedence as the lifecycle:

```python
credential_default = getattr(credential, "default_model", None)
if not isinstance(credential_default, str) or not credential_default.strip():
    credential_default = None
override = env_override or credential_default
effective_model = resolve_model(engine, provider, mode, override, ...)
```

This prevents a valid admin-curated `default_model` from being falsely classified as `unknown_model` or `stale_default`.

**`has_override` is keyed on `env_override` only** (not on `credential_default`). This keeps the badge CTA accurate: the `frozen_override` cause means "the user pinned a model they should edit/clear", not "an admin set a default". When the effective model comes from a credential default, the cause will be `stale_default` (if the model is unavailable), which maps to "Restart to use the current model" — the right action for a credential default that has gone stale.

---

## `ExternalAccountConfigService` (`external_account_config_service.py`)

Singleton: `external_account_config_service`

**`build_config(session, user) -> AccountConfigResponse`**

1. `SELECT AICredential WHERE owner_id == user.id ORDER BY created_at ASC`
2. For each credential: `AICredentialsService.decrypt_credential(credential) → AICredentialData`; then `_to_provider(credential)` to build an `AccountConfigProviderPublic`
3. A credential that fails decryption is skipped (warning log, no crash) so a single corrupt row cannot block login bootstrap
4. Calls `_resolve_default_credential_id(session, user)` for the `default_provider_credential_id` field

**`_to_provider(credential)` — provider map:**

| `AICredentialType` | `display_name` | `descriptor_slug` |
|--------------------|----------------|-------------------|
| `ANTHROPIC` | `"Claude"` | `"claude"` |
| `OPENAI` | `"OpenAI"` | `"openai"` |
| `GOOGLE` | `"Gemini"` | `"gemini"` |
| `OPENAI_COMPATIBLE` | `credential.name` (free-form) | `"openai-compatible"` |
| `MINIMAX` | `"MiniMax"` | `"minimax"` |

**`_to_provider(credential, data)`** builds `suggested_models` as:

```python
suggested_models = credential.available_models or credential.discovered_models or []
```

Curated wins; if curated is `None`/empty, falls back to `discovered_models`.

**`_resolve_model(cred_type, credential_model, discovered_models, default_model, available_models) -> str | None`**

Resolution chain (native clients call the provider API directly — must be a concrete ID):
1. `default_model` (admin curated) — when set and `not is_known_word(default_model)` (tier words are dropped because the native client can't use "haiku" against the provider API); prefix-stripped.
2. `credential_model` if set (always concrete; only `openai_compatible` has it stored)
3. `_strip_provider_prefix(available_models[0])` if `available_models` is non-empty
4. `_strip_provider_prefix(discovered_models[0])` if the list is non-empty
5. `resolve_model(engine, provider, mode="building", ...)` from `model_catalog` — but only if `not is_known_word(result)` (drops tier words like `"haiku"`, `"sonnet"` that are Claude Code internal shortcuts, not Anthropic API model IDs)
6. `None`

---

## `AICredential` Child Columns

**File:** `backend/app/models/credentials/ai_credential.py`

Five columns relevant to the managed-credential feature:

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `is_admin_managed` | `BOOLEAN` | NOT NULL, server_default false | Behavioral discriminator: row is read-only for owner when `True` |
| `managed_by_id` | `UUID` | nullable, FK → `user.id` ON DELETE SET NULL | Audit-only. Which admin provisioned this row; NULL when not admin-provisioned, or when the admin account was later deleted |
| `managed_credential_id` | `UUID` | nullable, FK → `managed_ai_credential.id` ON DELETE SET NULL | Structural link to parent. NULL = not a managed child, or parent deleted out-of-band |
| `default_model` | `VARCHAR(255)` | nullable | Mirror of parent's admin-curated default model. NULL for self-created credentials. Written through by reconcile; read by SDK resolution and native config. |
| `available_models` | `JSON` | nullable | Mirror of parent's admin-curated selectable model list. NULL for self-created credentials. Written through by reconcile; read by model pickers and native config. |

`default_model` and `available_models` are **absent** from `AICredentialCreate` and `AICredentialUpdate` — users cannot set them through the user-facing CRUD. They are projected read-only on `AICredentialPublic` so the owner UI and SDK resolution can read them.

---

## Read-Only Guard in `AICredentialsService`

**File:** `backend/app/services/credentials/ai_credentials_service.py`

Two methods gained an `admin_override: bool = False` keyword argument:

```
update_credential(session, credential_id, user_id, data, *, admin_override=False)
    # After fetching the row:
    if credential.is_admin_managed and not admin_override:
        raise HTTPException(403, "This credential is managed by your administrator and cannot be modified.")

delete_credential(session, credential_id, user_id, force=False, *, admin_override=False)
    # Same guard
```

User-facing routes in `ai_credentials.py` call these **without** `admin_override` → users blocked. `ManagedAICredentialsService` calls them **with** `admin_override=True` → reconcile passes.

`set_default` is NOT guarded — setting an admin-managed credential as one's default is a read-only use (beneficial for the user), not a modification.

---

## `AICredentialPublic` Projection

`AICredentialsService._to_public(credential, session)` projects `is_admin_managed` from the row:

```
is_admin_managed=credential.is_admin_managed,
```

`managed_by_id` and `managed_credential_id` are deliberately NOT projected on `AICredentialPublic` (user-facing) to avoid leaking admin identity or internal parent ID to the credential owner.

---

## `AccountConfigProviderPublic` (no table)

File: `backend/app/models/external/account_config.py`

| Field | Type | Notes |
|-------|------|-------|
| `credential_id` | `uuid.UUID` | The source `AICredential.id` |
| `provider_type` | `AICredentialType` | |
| `display_name` | `str` | Human-readable — `"Claude"`, `"OpenAI"`, `"Gemini"`, `"MiniMax"`, or the credential's own name for `openai_compatible` |
| `descriptor_slug` | `str` | Stable slug: `"claude"` / `"openai"` / `"gemini"` / `"minimax"` / `"openai-compatible"` |
| `base_url` | `str \| None` | Endpoint override (for `openai_compatible` and `google`) |
| `model` | `str \| None` | Suggested concrete model ID (see model resolution) |
| `api_key` | `str` | **Decrypted key** — the security boundary |
| `is_default` | `bool` | Whether this is the user's default for its type |
| `is_admin_managed` | `bool` | Whether the row was admin-provisioned |
| `default_chat_mode_label` | `str` | Same as `display_name` — label the native app uses for the auto-created chat mode |
| `suggested_models` | `list[str]` | `credential.available_models` when non-empty (admin curated); otherwise `credential.discovered_models`; empty if neither is set |

## `AccountConfigResponse` (no table)

| Field | Type | Notes |
|-------|------|-------|
| `providers` | `list[AccountConfigProviderPublic]` | All owned credentials; empty when user has none |
| `default_provider_credential_id` | `uuid.UUID \| None` | Resolved conversation-default credential for the user |
| `generated_at` | `datetime` | UTC timestamp when the bundle was assembled |

---

## Security Events

All audit events contain counts/IDs but **never** key bytes.

| Event type | Scope | Severity | Emitted by |
|------------|-------|----------|------------|
| `admin.ai_credential.provision` | Child owner | `medium` | Per added child — `POST /` and `PATCH /{id}` |
| `admin.ai_credential.update` | Child owner | `medium` | Per mutated child — `PATCH /{id}` (no-op children emit no event) |
| `admin.ai_credential.delete` | Child owner | `medium` | Per removed child — `PATCH /{id}` and `DELETE /{id}` |
| `admin.ai_credential.set_default` | Child owner | `medium` | Per member — `POST /{id}/set-default` |
| `admin.managed_ai_credential.create` | Admin | `medium` | `POST /` — one per call |
| `admin.managed_ai_credential.update` | Admin | `medium` | `PATCH /{id}` — one per call |
| `admin.managed_ai_credential.delete` | Admin | `medium` | `DELETE /{id}` — one per call |
| `admin.managed_ai_credential.apply_to_existing` | Admin | `medium` | `POST /{id}/apply-to-existing` — real run only; a dry run emits nothing |
| `admin.ai_credential.auto_provision` | **New owner** | `low` | `AccountProvisioningService`, per child granted at account creation. `details = {managed_credential_id, child_credential_id, target_user_id, origin, role, managed_by_id, actor: "system"}` |
| `admin.ai_credential.auto_provision_failed` | **New owner** | `medium` | `AccountProvisioningService`, per parent that could not be granted. `details = {managed_credential_id, target_user_id, origin, role, reason, actor: "system"}` |
| `admin.ai_credential.mint_requested` | Member | `medium` | A member added to a **minted** record (in place of `provision`, which names a credential), and each explicit Retry — the retry carries `retry_of: <last_error>` |
| `admin.ai_credential.minted` | Member | `medium` | `KeyProvisioningService`, on a successful mint. `details = {managed_credential_id, child_credential_id, target_user_id, external_key_ref}` |
| `admin.ai_credential.mint_failed` | Member | `medium` | Per failed attempt. `details = {managed_credential_id, target_user_id, reason, attempts, terminal}` — `terminal` distinguishes a backoff from a give-up |
| `admin.ai_credential.revoked` | `audit_user_id` | `medium` | A key destroyed at the provider |
| `admin.ai_credential.revoke_failed` | `audit_user_id` | **`high`** | A key we could **not** destroy. **This event is the durable record** — the membership row that carried the handles is gone by then (delete-then-revoke ordering), so without it the key is live at the provider with nothing naming it. The external ref goes in deliberately |
| `admin.ai_credential.revoke_blocked` | Member | **`high`** | A key deliberately left live because its child could not be deleted (`in_use_bundle`, `delete_failed`). Written from synchronous code by constructing the row directly, for the same reason `AccountProvisioningService` does |
| `admin.provider_admin_credential.create` | Admin | `medium` | `details = {provider_admin_credential_id, provider_type, project_id, spend_limit_cents}` |
| `admin.provider_admin_credential.update` | Admin | `medium` | `details = {provider_admin_credential_id, secret_rotated: bool}` — **whether** the secret was replaced, never what with |
| `admin.provider_admin_credential.delete` | Admin | `medium` | `details = {provider_admin_credential_id, forced, minting_credential_count, live_minted_key_count}` — what a force **overrode**, not merely that it was used. This is the one action that permanently strands live provider keys, and this row is the last place anyone can learn how many |
| `admin.provider_admin_credential.verify` | Admin | `medium` | `details = {provider_admin_credential_id, ok, spend_limit_enforcing, error}` |
| `admin.provider_admin_credential.apply_spend_limit` | Admin | `medium` | `details = {provider_admin_credential_id, ok, spend_limit_cents, error}` |
| `external.account_config.read` | Calling user | `high` | `GET /external/account-config` (successful call only) |

`external.account_config.read` details: `{client_kind, external_client_id, provider_count, credential_ids}`.

The old `admin.ai_credential.provision_batch` event type from the previous per-row model is gone.

Automatic grants are **not** emitted by the admin route — they have no acting admin. They are siblings of `admin.ai_credential.provision` in the same namespace (different actor) so a reader of the security feed can tell an automatic grant from an admin's deliberate one without decoding the details blob. No key material is recorded in either.

**External key refs are recorded on purpose** in every minting and revocation event: they are what makes a leaked key nameable. Key material never is, anywhere.

`admin.ai_credential.auto_provision` now omits `child_credential_id` entirely when the grant is a membership of a minted record, rather than stringifying a `None` into a field every other row of the feed reads as an id. `set_default` events likewise skip members with no key.

**Whose feed a revoke lands in** is `RevocationRequest.audit_user_id`, not always the key holder — see [`RevocationRequest.audit_user_id`](#revocationrequestaudit_user_id).

---

## Model Catalog Integration

`ExternalAccountConfigService._resolve_model` imports from `backend/app/services/environments/model_catalog.py`:
- `resolve_model(engine, provider, mode, override, openai_compatible_model)` — returns the catalog default for a provider/engine combination
- `is_known_word(model_string)` — returns `True` for SDK-internal tier words (`"haiku"`, `"sonnet"`, `"opus"`) that cannot be used as Anthropic API model IDs
- `_strip_provider_prefix(model_string)` — strips the `provider/` prefix from discovered model IDs (e.g., `"anthropic/claude-3-5-sonnet-..."` → `"claude-3-5-sonnet-..."`)

---

## Dependencies

| Dependency | Used by |
|-----------|--------|
| `get_current_active_superuser` | All `/admin/llm-providers/`, `/admin/provider-admin-credentials/` and `/admin/provider-adapters/` routes |
| `CurrentUser` | `/external/account-config`, `GET /ai-credentials/provisioning` |
| `CurrentClientClaims` | `/external/account-config` (reads `client_kind`, `external_client_id` from JWT) |
| `SessionDep` | All routes |

`CurrentClientClaims` is defined in `backend/app/api/deps.py` and returns `(client_kind, external_client_id)` from the JWT. It is shared with the rest of the external A2A surface.

---

## Tests

| File | Covers |
|------|--------|
| `backend/tests/api/ai_credentials/minted_ai_credentials_test.py` | 31 tests — minted-record create/update validation, membership statuses, converge, retry, revocation, the account lifecycle cascade, the owner-facing list and `api_key_onboarding_state` |
| `backend/tests/api/ai_credentials/provider_admin_credentials_test.py` | 14 tests — secret write-only-ness, verify, apply-spend-limit, the delete gate and its force override |
| `backend/tests/architecture/provider_adapter_registry_test.py` | 11 tests — the per-provider-table property and the adapter contract |
| `backend/tests/unit/ai_provider_adapters_test.py` | 31 tests — per-adapter behaviour |
| `backend/tests/utils/key_provisioning.py` | `converge_keys(db, *, limit=None)`, `make_membership_due(db, user_id)`, `mark_membership_minting(db, user_id)` |
| `backend/tests/utils/ai_provider.py` | `stub_all_providers`, `stub_minting_providers`, `probe_success` / `probe_skip` / `probe_invalid_key` |
| `backend/tests/stubs/key_provisioner_stub.py` | Subclasses `OpenAIKeyProvisioner` and overrides **`_call` alone**, so the spend-cap predicate, the null-secret rejection and the external-ref shape all still run for real. Installed through `registry.override_for_tests`, not `unittest.mock.patch`, and it **counts its invocations** so "the stub was never reached" fails loudly instead of passing quietly while the suite talks to the real provider |
| `backend/tests/stubs/provider_adapter_stub.py` | The recording adapter stub |

The `share_credential` refusal **is** now covered. It could not be reached through a route (`AICredentialShare` is unreachable from any route; the only caller is bundle install), so it is exercised **bundle-shaped** — through the install path that actually calls it — and **parametrized over both provisioning modes**, `minted` and `shared`, because the predicate is `is_admin_managed` and treating one mode as the interesting one is what produced the earlier gap. See `backend/tests/api/agents/bundles_install/agents_bundles_install_readiness_test.py` for the `publisher_credential_unshareable` gate verdict.

**Still not covered, knowingly:** the `adapter is None` branch of `detect_anthropic_credential_type` (`backend/app/utils.py`). It is marked `# pragma: no cover - the registry always serves it` and left **documented as unreachable** rather than covered by a test that would assert nothing — reaching it needs the registry to fail to serve a shipped provider, and a test that arranges that is testing the arrangement.

---

*Last updated: 2026-09-06 — zero-touch onboarding phase 5, **fifth pass** (review of the fourth pass's fixes): the `AICredentialShare` lookup moved **before** `is_shareable` in `InstallReadinessGate._scan_ai_credentials` and `InstallService._linkable_publisher_ai_credential` (an installer who already holds a share keeps the credential and is reported as missing nothing; existing share rows are left in place as an accepted trade); `PublishService.publisher_ai_credential_notices` → the required `AgentBundleRevisionPublic.publish_notices`, wired only by `POST /agents/{id}/publish` and rendered as a dismissible callout on the Bundle tab; `UserPublicWithAICredentials.api_key_onboarding_state` made **required with no default**, with the dashboard's `?? "needs_key"` removed, the wall latch re-based on `credentialsStatus !== undefined`, and the banner no longer suppressed by "Skip for now"; `_linkable_publisher_ai_credential` returns `None` for a vanished row (the FK made the old fallback claim an `IntegrityError`); `ai_credentials_service.has_a_default` deleted. See [Known Gap 14](admin_ai_credential_provisioning.md#known-gaps) for the escalated, deliberately unfixed unshareable-publisher-credential block.*

*Previously — **fourth pass** (whole-feature seam review): `is_shareable` widened from minted-only to every admin-managed child and raised as the typed `AICredentialNotShareableError`; `InstallService._linkable_publisher_ai_credential` made the documented install fallback real; the `publisher_credential_unshareable` gate reason, with `GateMissingReason` moved into `app/models/bundles/catalog.py`; `ManagedReconcileBlock.message` and its reason→sentence table; `AIKeyOnboardingState` made provider-agnostic via `owner_ids_with_a_default`, with the member-level projection and the latched dashboard wall; the mint claim token and its three race tests; `holds_provider_key` as the one key-holding predicate; the restored `ids_from_openai_shape` raise. Phase 5 itself: the `ai_providers` adapter registry, `provider_admin_credential`, `managed_ai_credential_membership` (membership as a row, with a backfill), `provisioning_mode` on the parent, `KeyProvisioningService` + its converge scheduler, the deactivation/reactivation/deletion revocation cascade, `GET /admin/provider-adapters/`, the member-retry route, `GET /ai-credentials/provisioning`, `api_key_onboarding_state`, and `app.core.db.leader_session`; migration `ed8d6a23f13c`. Phase 4 renamed the admin UI route to `/admin/ai-credentials` (backend prefix, tag, service and cache key unchanged). Phase 2: `auto_provision_roles` + per-mode model overrides (migration `b71863b32aa1`), `add_members` / `apply_to_existing`, `(role, mode)` slot-conflict validation, `AccountProvisioningService`*
