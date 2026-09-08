# AI Providers, Admin-Provisioned AI Credentials + Native Account-Config — Technical Details

## File Locations

### Backend

**Models:**
- `backend/app/models/credentials/managed_ai_credential.py` — `ManagedAICredential` (parent table); `ManagedAICredentialCreate`, `ManagedAICredentialUpdate`, `ManagedAICredentialPublic` (admin DTOs); `ManagedAICredentialMember`, `ManagedAICredentialReconcileResult`, `ManagedReconcileSkip`, `ManagedReconcileBlock` **and `MANAGED_RECONCILE_BLOCK_MESSAGES`**, the reason→sentence table that sits beside the model
- `backend/app/models/credentials/managed_ai_credential_membership.py` — **new.** `ManagedAICredentialMembership` (table), `MembershipProvisioningStatus` (enum), `CONVERGEABLE_STATUSES`, `UserKeyProvisioningPublic` (owner-facing DTO), and the pair `holds_provider_key(membership)` / `holds_provider_key_clause()` — the one predicate for "does this member hold a key at the provider", in its Python and SQL forms
- `backend/app/models/credentials/provider_admin_credential.py` — **the `ai_provider` table lives here.** `AIProvider` (table, `__tablename__ = "ai_provider"`), `AIProviderKind`, `ProviderAdminCredentialConfig` (aliased `AIProviderConfig`), `AIProviderConfigInput`, and the wire shapes `AIProviderPublic` / `AIProviderCreate` / `AIProviderUpdate` / `AIProviderRotateKey` / `AIProviderVerifyResult` / `AIProviderDeleteImpact` / `AIProviderDeleteMember`. **The module filename was deliberately not renamed with the table** — a file move is a diff nobody can read against a rename this size; the module docstring is the thing that had to change, and it did (see [The dual-meaning secret](#the-dual-meaning-secret-column)). The four `ProviderAdminCredential*` DTOs are gone with the route that used them
- `backend/app/services/credentials/provisioning_policy.py` — `ProvisioningPolicy` (frozen dataclass), `resolve_policy` / `resolve_policies`, `policy_from_provider` / `policy_from_manual`, and `POLICY_MODES`. **The only legal read path for a managed credential's wiring policy** — see [The policy resolver](#the-policy-resolver-provisioning_policypy)
- `backend/app/models/credentials/provider_adapter.py` — **new.** `ProviderAdapterPublic`, `ProviderAdaptersPublic` — the API projection of an adapter's own declarations. See [provider_adapters_tech](provider_adapters_tech.md)
- `backend/app/models/credentials/ai_credential.py` — `AICredential` (child link: `managed_credential_id` FK, ON DELETE SET NULL; existing `is_admin_managed` and `managed_by_id` columns unchanged); `AICredentialPublic` (`is_admin_managed` projection). The `AdminAICredentialPublic` / `AdminAICredentialCreate` / `AdminProvisionSkip` / `AdminAICredentialProvisionResult` DTOs were **deleted** with the legacy service (below) — no admin-facing projection of `AICredential` exists any more
- `backend/app/models/users/user.py` — `AIKeyOnboardingState` (enum: `has_key` / `preparing` / `needs_key`, **provider-agnostic**); `UserPublicWithAICredentials.api_key_onboarding_state` (**required, no default** — see [Why it is required](#why-api_key_onboarding_state-is-required-with-no-default)). The same enum is also carried per member on `ManagedAICredentialMember.api_key_onboarding_state` (**no default**)
- `backend/app/models/external/account_config.py` — `AccountConfigProviderPublic`, `AccountConfigResponse` (native-config response models)

**Routes:**
- `backend/app/api/routes/admin_llm_providers.py` — credential-oriented `POST/GET/PATCH/DELETE /admin/llm-providers/`, `POST /admin/llm-providers/{id}/set-default`, `POST /admin/llm-providers/{id}/members/{user_id}/retry`, `POST /admin/llm-providers/test-connection`; superuser-gated. Path and tag are unchanged so the Managed Credentials surface does not churn. **`POST /{id}/apply-to-existing` is gone** — the action moved to the Provider. The `(role, mode)` uniqueness rule and its `409` envelope are gone from this module too, moving to `/admin/ai-providers` with the `auto_provision_roles` they guard; `admin_ai_providers._conflict_409` is the handler and it inherited this module's envelope shape verbatim, which is why the wire keys still say `credential`
- `backend/app/api/routes/admin_ai_providers.py` — prefix `/admin/ai-providers`, tag `admin-ai-providers` (generated service `AdminAiProvidersService`). Superuser-gated end to end. Carries `GET /adapters`, declared **above** `/{provider_id}` so the literal wins the match. `_audit(...)` writes one `SecurityEvent` per act, keyed to the acting admin; `_conflict_409(exc)` renders `AIProviderConflictError` as the structured `auto_provision_conflict` body. **Replaces `admin_provider_credentials.py`, which is deleted** along with both routers it declared
- `backend/app/api/routes/ai_credentials.py` — adds `GET /ai-credentials/provisioning`, **declared above `/{credential_id}`** so the literal path is matched before the UUID route claims it
- `backend/app/api/routes/users.py` — `get_ai_credentials_status` gains `api_key_onboarding_state`; `update_user` detects an `is_active` **transition** (via `exclude_unset` + a before-snapshot, never truthiness — `is_active` defaults to True, so a truthiness check would read every silent PATCH as "activate") and calls `on_account_reactivated` / `on_account_deactivated`; `delete_user` and `delete_user_me` snapshot revocations before `session.delete` and schedule them after the commit. The AI-functions OAuth guard now asks `adapter.issues_oauth_tokens` **before** decrypting, instead of testing `expected_type == ANTHROPIC` and `startswith("sk-ant-oat")`
- `backend/app/api/routes/external_account_config.py` — `GET /external/account-config`; native-token-gated
- `backend/app/api/main.py` — routers registered (`admin_llm_providers.router`, `admin_ai_providers.router`, `external_account_config.router`). The two admin prefixes do not overlap

**Services:**
- `backend/app/services/credentials/managed_ai_credentials_service.py` — `ManagedAICredentialsService` (singleton: `managed_ai_credentials_service`); owns parent CRUD + reconcile + `add_members` + `apply_to_existing`; defines `ManagedCredentialConflictError` and the `MemberAddition` dataclass
- `backend/app/services/credentials/key_provisioning_service.py` — **new.** `KeyProvisioningService` (singleton: `key_provisioning_service`); owns the membership *provisioning lifecycle* — `converge`, `revoke_now`, `schedule_revocations`, `collect_user_revocations`, `suspend_user_memberships`, `resume_user_memberships`, `requeue_failed_member`, `list_user_provisionings`, `api_key_onboarding_state`; `ConvergeReport` dataclass; the `EVENT_MINTED` / `EVENT_MINT_FAILED` / `EVENT_REVOKED` / `EVENT_REVOKE_FAILED` / `EVENT_REVOKE_BLOCKED` constants
- `backend/app/services/credentials/key_provisioning_types.py` — **new.** `RevocationRequest` only. Its own module so the one real dependency between the managed-credential service and the provisioning service points in a single direction: the import cycle is a function-local import in exactly one place rather than two modules importing each other at module level
- `backend/app/services/credentials/key_provisioning_scheduler.py` — **new.** APScheduler `BackgroundScheduler`, 1-minute interval, `max_instances=1`, `coalesce=True`; `KEY_PROVISIONING_LOCK_KEY = 0x4B45594D494E54` ("KEYMINT"); submits each sweep to the main event loop via `asyncio.run_coroutine_threadsafe` with a fixed `SWEEP_WAIT_TIMEOUT_SECONDS = 180`
- `backend/app/services/credentials/ai_providers_service.py` — `AIProvidersService` (singleton: `ai_providers_service`); provider CRUD, `verify`, `rotate_key`, `apply_to_existing`, the ordered `async delete`, `decrypt_secret` / `fixed_key_envelope`, `auto_provision_targets` / `grant_targets`, `write_policy_fields`, `to_public`; `AIProviderConflictError` (a subclass of `ManagedCredentialConflictError`), `AIProviderInUseError`, `ProviderGrantTarget`. **Replaces `provider_admin_credentials_service.py`, which is deleted**
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
- `backend/app/alembic/versions/ed8d6a23f13c_add_membership_row_provider_admin_.py` — `down_revision = 'adffe56ef505'`. Creates `provider_admin_credential` and `managed_ai_credential_membership`; adds `provisioning_mode` + `provider_admin_credential_id` to `managed_ai_credential` and makes its `encrypted_data` nullable; **backfills membership rows**. The same three `cli_device_login_request` alterations were re-proposed and dropped again
- `backend/app/alembic/versions/c23d6b59a8f5_rename_provider_admin_credential_to_ai_.py` — **the split.** `down_revision = 'ed8d6a23f13c'`. Renames `provider_admin_credential` → `ai_provider`, adds the rule and the wiring policy, re-points and re-constrains `managed_ai_credential.provider_id`, backfills, and drops the two columns the credential no longer owns. See [The two migrations, as a chain](#the-two-migrations-as-a-chain)
- `backend/app/alembic/versions/45938a69aee7_add_claim_held_default_slots_to_managed_.py` — `down_revision = 'c23d6b59a8f5'`. Adds `managed_ai_credential_membership.claim_held_default_slots`, `false` for every existing row

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
- `providerTypes.ts` — `PROVIDER_TYPE_OPTIONS` array (Anthropic/OpenAI/OpenAI Compatible/Google — MiniMax omitted); `getProviderTypeLabel` helper; `MANAGED_CREDENTIALS_QUERY_PREFIX = ["admin", "llm-providers"]` — **deliberately not renamed with the page**: it is a cache key, not a route path, and three surfaces (this page, the Company AI credentials card, the invite wizard) depend on producing the *same* string. Renaming it splits the cache silently — no error, just two lists that stop agreeing; `managedCredentialsQueryKey(targetUserId?)` factory. Phase 2 adds `SDK_MODE_OPTIONS` / `sdkModeLabel`, the `AutoProvisionConflict` interface, `parseAutoProvisionConflict(error)` (checks every field, not just `code`, so a future 409 shape cannot render a sentence containing `undefined`), `describeAutoProvisionConflict(conflict)` (recomposes the message with display labels), and `findAutoProvisionConflict(records, record, role)` — the advisory client-side mirror of the backend rule
- `frontend/src/components/Admin/AccessPolicy/CompanyAiCredentialsCard.tsx` — **replaces `AutoProvisionedCredentialsMatrix.tsx`** (deleted in the Access-tab redesign). Now its own half-width card on the Access tab rather than nested at the foot of a single policy card: a `PreviewList` of `CompanyAiCredentialRow.tsx` rows — credential name, provider type on the metadata line, and a three-segment `ToggleGroup type="multiple"` (`components/ui/toggle-group.tsx`, added for this card) with short labels from `USER_ROLE_OPTIONS[].short` and the full role name on `aria-label` + `Tooltip`; the segment is styled off `aria-pressed`, because a `TooltipTrigger asChild` overwrites Radix Toggle's `data-state`. Reads `managedCredentialsQueryKey()` (the same key the AI Credentials page uses, `staleTime` 30s); each toggle is a `PATCH` carrying only `auto_provision_roles`, writing the returned row straight into the cache before invalidating so a second toggle can never compute `next` off a stale list; capped at **5 rows** (granted-first, then alphabetical) with a footer link to `/admin/ai-credentials` reading "Manage AI credentials" or "Show all (N) on AI Credentials"; the **row** whose PATCH is in flight is frozen (a per-record id set held by the card — all rows share one mutation observer, so `isPending` alone would thaw row A when row B is clicked and reopen the stale-read window) while its neighbours stay live; the advisory client-side conflict is a tooltip on the affected segment; the 409 renders as a destructive `Alert` under the list, naming both the segment clicked and the credential that owns the conflicting default. See [Access Policy — tech](../server_configuration/access_policy_tech.md)
- `frontend/src/utils/userRoles.ts` — **new.** `USER_ROLE_OPTIONS` (capability order: agent-user → agent-developer → admin) and `userRoleLabel(role)`. Takes a bare `string`, not `UserRoleValue`, so a server that learns a fourth role renders its identifier instead of crashing. `NewUserDefaultsCard`, `LlmProvidersTable`, `LlmProviderActionsMenu`, the dialog and `CompanyAiCredentialsCard` all read from here
- `frontend/src/components/Common/ListModelsButton.tsx` — gains an optional `probeModels?: () => Promise<AICredentialTestResult>` prop. When given, `credentialId` / `credentialType` are unused and the caller gates the button with `disabled`. Needed because the admin dialog holds a *parent record on a different endpoint*, or a key typed into the form and never persisted — neither is an `AICredential` id. The dialog passes a probe that does **not** go through the shared Test Connection mutation, so opening the picker never repaints the Test Connection banner and the picker's own Retry cannot re-enter a pending mutation

**Phase 5 frontend additions:**
- `frontend/src/routes/_layout/admin/ai-credentials.tsx` — **two hash-addressed tabs**, `#managed-credentials` (default) and `#providers`, through `Common/HashTabs.tsx` (which gained an optional `onTabChange` so the route can put each tab's own primary button in the page header). The `?newCredentialFor=` strip carries the current hash forward, so dismissing a hand-off does not bounce the admin back to the default tab. Each list polls every 10s while it holds a key in flight
- `frontend/src/components/Admin/AiProviders/` — the Providers tab and everything on it: `ProvidersTab.tsx`, `ProviderRow.tsx`, `ConnectProviderDialog.tsx` (the three-step wizard), `EditProviderSheet.tsx`, `ProvisioningPolicyFields.tsx` (the policy block shared by the wizard's third step and the sheet's third section), `ReplaceProviderKeyDialog.tsx`, `ApplyProviderDialog.tsx`, `DeleteProviderDialog.tsx`, `aiProviderErrors.ts`. **`Admin/ProviderAdminCredentials/` is deleted**, both files, not migrated
- `frontend/src/components/Admin/LlmProviders/useProviderAdapters.ts` — `PROVIDER_ADAPTERS_QUERY_KEY`, `useProviderAdapters(enabled)`, and `adminConfigFields(adapter)` (parses `admin_config_schema.fields`, dropping malformed entries). Reads `AdminAiProvidersService.listProviderAdapters`. `supportsMinting` compares `=== true`, so *loading* is false rather than optimistically permissive — loading is not a yes
- `frontend/src/components/Admin/LlmProviders/MemberKeyStatus.tsx` — **new.** `hasKeyInFlight(record)`, `<KeyStatusSummary>` (the **Keys** column: "Shared key", or a per-status roll-up with a spinner), `<MemberChip>` (member pill + status label + failure reason + **Retry**, calling `retryMemberKeyProvisioning`)
- `frontend/src/components/UserSettings/KeyProvisioningRows.tsx` — **new.** `useMyKeyProvisionings()` (polls every 10s **only while something is in flight**, and invalidates the credential list and status queries when the in-flight count drops) and the two owner-facing rows
- `frontend/src/utils/keyProvisioning.ts` — **new.** `MY_KEY_PROVISIONINGS_QUERY_KEY`, `MEMBERSHIP_STATUS_META` (an exhaustive `Record<MembershipProvisioningStatus, …>` carrying an admin label, an owner label, a tone and `inFlight` — the exhaustive record is what makes a new server status a **type error** rather than a blank cell), and `describeProvisionError(code)` (unknown codes render verbatim rather than vanishing)
- `ManagedCredentialDialog.tsx` — three props (`initialTargets`, `nameSubject`, `onCreated`) so the invite success screen can seed it; `isControlled` replaces `mode === "edit"` for open-state ownership. The **Key source** radio group, the **Provider organisation** select and the **Auto-provision** card are **gone**: `ManagedAICredentialCreate` / `Update` set `extra="forbid"`, so the fields behind them are no longer sendable. On `is_provider_owned` the dialog renders a different composition — an alert naming the Provider, the Provider's decisions as read-only **text** rather than disabled inputs, and the member picker as the one editable control
- `InviteSuccessPanel.tsx` — **replaces the nested "Add a key for this user" card.** A guideline-driven redesign of the invite success screen moved this from a card opening `ManagedCredentialDialog` in place to a plain `Button variant="link"` reading "Add an AI key for `<email>`", navigating to `/admin/ai-credentials?newCredentialFor=<user.id>&label=<label>` (hidden for an inactive account, same condition as before)
- `routes/_layout/admin/ai-credentials.tsx` — reads `newCredentialFor` / `label` off `validateSearch`, opens `ManagedCredentialDialog` **controlled** with `initialTargets=[{id, userId, fallbackLabel: label}]` and `nameSubject=label` when present, then strips both params (the `?new=1` latch idiom from `credential/$credentialId.tsx`, re-armed per target id so a second hand-off to a different person opens again). `onCreated` sets a one-line result under the page header from the created record's own member row (`api_key_onboarding_state`): `has_key` → "Key added."; `preparing` → "The key is being created now."; `needs_key` → an amber alert, "Key added — not their default."
- `routes/_layout/index.tsx` — the dashboard wall reads `api_key_onboarding_state`, not `has_anthropic_api_key`. It polls every 10s until the answer is `has_key` (both `preparing` **and** `needs_key` poll — see [The dashboard wall](#the-dashboard-wall-latch-banner-and-polling)), and the full-page form is **latched to first render** via `wallAllowedRef`. **There is no loading fallback** — the field is required on the server's response, so the browser reads `credentialsStatus?.api_key_onboarding_state` with no `?? "needs_key"` and `undefined` means only "the server has not answered"

**Generated client services used:**
- `AdminLlmProvidersService` — `createManagedAiCredential`, `listManagedAiCredentials`, `getManagedAiCredential`, `updateManagedAiCredential`, `deleteManagedAiCredential`, `setManagedAiCredentialDefault`, `testManagedAiCredentialConnection`, `retryMemberKeyProvisioning`
- `AdminAiProvidersService` — `listAiProviders`, `createAiProvider`, `getAiProvider`, `updateAiProvider`, `deleteAiProvider`, `verifyAiProvider`, `rotateAiProviderKey`, `applyAiProviderToExisting`, `listProviderAdapters`
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
| `encrypted_data` | `TEXT` | **nullable** (was NOT NULL; relaxed by `ed8d6a23f13c`) | Fernet-encrypted JSON `{api_key, base_url?, model?}`. The real key for a **manual** record. **NULL on a provider-owned row** — the provider is the source of truth for the key — and NULL for a minted record, which has no shared key at all. Every reader goes through `_decrypt_parent`, which **prefers the provider** rather than falling back to it: a fallback would let a stale copy left on this column re-key every member on the next rotation while reporting success |
| ~~`provisioning_mode`~~ | — | **dropped by `c23d6b59a8f5`** | Derived now: no provider → `shared`; a `fixed_key` provider → `shared`; a `minted` provider → `minted`. `ManagedAICredentialPublic.provisioning_mode` stays on the wire, computed through the policy resolver, so a record and its provider cannot disagree about it |
| ~~`auto_provision_roles`~~ | — | **dropped by `c23d6b59a8f5`** | The rule is a property of a provider. A managed credential is no longer a factory |
| `provider_id` | `UUID` | nullable, FK → `ai_provider.id` ON DELETE **RESTRICT** (`fk_managed_ai_credential_provider`) | The provider that owns this record. **NULL = a manual record**, which is its own source of truth. Renamed from `provider_admin_credential_id` by `c23d6b59a8f5`, which also swapped SET NULL for RESTRICT — see [Why RESTRICT](#why-restrict-and-not-set-null) |
| `base_url` | `VARCHAR(500)` | nullable | Non-secret mirror for projection/UI |
| `model` | `VARCHAR(255)` | nullable | Non-secret mirror for projection/UI |
| `default_model` | `VARCHAR(255)` | nullable | **Shadowed.** Curated default model (bare concrete id). Added by migration `c1a4b2d3e5f6`. |
| `available_models` | `JSON` | nullable | **Shadowed.** Curated selectable model list. Added by migration `c1a4b2d3e5f6`. |
| `set_as_default` | `BOOLEAN` | NOT NULL, server_default false | **Shadowed.** Whether each child is set as its owner's default |
| `set_user_sdk_defaults` | `BOOLEAN` | NOT NULL, server_default false | **Shadowed.** Whether each owner's SDK-default pointers are wired |
| `sdk_default_modes` | `JSON` | NOT NULL, server_default `["conversation","building"]` | **Shadowed.** Modes to wire |
| `model_override_conversation` | `VARCHAR(255)` | nullable | **Shadowed.** Model pinned on each member's `User.default_model_override_conversation`. Added by `b71863b32aa1`. |
| `model_override_building` | `VARCHAR(255)` | nullable | **Shadowed.** Same for building. Added by `b71863b32aa1`. |
| `expiry_notification_date` | `TIMESTAMP` | nullable | **Shadowed.** Informational expiry reminder |
| `managed_by_id` | `UUID` | nullable, FK → `user.id` ON DELETE SET NULL, index `ix_managed_ai_credential_managed_by` | Which admin owns/manages this record; NULL when the admin account is deleted |
| `created_at` | `TIMESTAMP` | NOT NULL | Creation time (UTC) |
| `updated_at` | `TIMESTAMP` | NOT NULL | Last update time (UTC) |

**"Shadowed" is a real state, not a comment.** Those eight columns are still there and still hold whatever they held. They are the **real values for a manual record** — one with `provider_id IS NULL` — and they are **not read at all** on a provider-owned row, where the provider's same-named columns are the answer. They are deliberately not mirrored down: copying the provider's policy here would make every existing read site work unchanged and would create a second copy of a number the provider owns, which goes stale the moment either side is edited. A stale copy displayed as the active policy is worse than no copy.

The branch between the two lives in exactly one place — see [The policy resolver](#the-policy-resolver-provisioning_policypy) — and two architecture tests hold it there.

#### Why RESTRICT and not SET NULL

Everywhere else in this domain the FK is SET NULL with a service-level gate, because losing a pointer should degrade a capability rather than delete data. Here the opposite is true. A provider-owned credential whose `provider_id` went NULL would silently become a **manual** credential: fully editable, its shadowed columns suddenly load-bearing and holding stale values, and its key column NULL. That is a worse state than a refused delete.

The service deletes the credential first and the provider second, so RESTRICT is the backstop under that ordering — getting the order wrong surfaces as a database error rather than as an orphan that looks manual. `tests/migrations/ai_provider_split_test.py::test_a_provider_cannot_be_deleted_out_from_under_its_credential` pins it.

The direction it does **not** cover is the other one: deleting the credential leaves the provider standing. That is closed in the service (`ManagedAICredentialsService.delete` refuses a provider-owned record with a `400` naming the provider) rather than by the schema, because the fix has to be a message an admin can act on.

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

### `ai_provider` table (renamed and extended from `provider_admin_credential` by `c23d6b59a8f5`)

Server-scoped, **no `owner_id`**. The shape is copied deliberately from `MailServerConfig`: server-scoped with an encrypted secret column, a public projection exposing `has_secret: bool` instead of the value, and a superuser-or-nothing router.

The table was **renamed, not replaced**, so every minted setup that existed before the split keeps working with no data movement.

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `id` | `UUID` | PK | |
| `name` | `VARCHAR(255)` | NOT NULL | Human label. What a refusal, an audit row and the credentials table's **Source** column point at |
| `kind` | `VARCHAR(16)` | NOT NULL, **DB** server_default `'minted'` | `AIProviderKind`: `fixed_key` \| `minted`. **No Python-side default** — a provider whose kind defaulted would let a caller create one without saying what its secret is. The database default exists only so the rename backfills history (every pre-existing row is an organisation administration secret) and is reachable only by a raw INSERT that omits the column |
| `provider_type` | `VARCHAR(50)` | NOT NULL, index `ix_ai_provider_type` | `AICredentialType`. **The column keeps its old name; the DTO exposes it as `type`** — renaming it is a migration the projection does not need, so the translation happens once, in the projection |
| `encrypted_secret` | `TEXT` | NOT NULL | Fernet. **Its meaning depends on `kind`** — see below. Never projected, never linked to an environment, never in `/external/account-config` |
| `config` | `JSON` | NOT NULL, server_default `'{}'::json` | `AIProviderConfig` (the class is still `ProviderAdminCredentialConfig`, aliased) — `organization_id?`, `project_id?`. Populated for `minted`; empty for `fixed_key`, which has no organisation or project to name |
| `base_url` | `VARCHAR(500)` | nullable | `fixed_key` + a type whose adapter sets `requires_base_url` |
| `model` | `VARCHAR(255)` | nullable | `fixed_key` + a type whose adapter sets `requires_model` |
| `auto_provision_roles` | `JSON` | NOT NULL, server_default `'[]'::json` | **The rule.** Roles whose *newly created* accounts receive a key from this provider. Empty is the only safe default: a provider that auto-granted itself the moment it was created would hand a company key to the next person who signs up, with no admin act anywhere in the story |
| `set_as_default` | `BOOLEAN` | NOT NULL, server_default false | |
| `set_user_sdk_defaults` | `BOOLEAN` | NOT NULL, server_default false | |
| `sdk_default_modes` | `JSON` | NOT NULL, server_default `["conversation","building"]` | |
| `default_model` | `VARCHAR(255)` | nullable | Written onto each child row |
| `available_models` | `JSON` | nullable | Written onto each child row |
| `model_override_conversation` | `VARCHAR(255)` | nullable | Written onto each member's **profile** for that mode |
| `model_override_building` | `VARCHAR(255)` | nullable | Same for building |
| `expiry_notification_date` | `TIMESTAMPTZ` | nullable | It describes the key, and the provider owns the key |
| `last_verified_at` | `TIMESTAMPTZ` | nullable | Last successful Verify |
| `last_verify_error` | `TEXT` | nullable | Coarse reason code for the last failed Verify. Both are non-secret and both are shown to the admin, because "the key works" and "the project is capped" are the two questions a Verify press exists to answer |
| `created_by_id` | `UUID` | nullable, FK → `user.id` ON DELETE **SET NULL** | Audit only. **Never CASCADE** — user deletion is a bare cascade, and deleting the superuser who pasted the key must not destroy the instance's ability to revoke every key it ever minted |
| `created_at` / `updated_at` | `TIMESTAMP` | NOT NULL | |

Indexes: `ix_ai_provider_type` on `provider_type` (renamed with the table), and `ix_ai_provider_auto_provision` on **`kind`** — not on the JSON roles column. The auto-provision scan reads every provider row and filters roles in Python, because there are tens of these rather than thousands; what an index can usefully narrow here is the kind, not the membership test.

**Invariants enforced in the service, not the schema**, because a schema-level CHECK would need a migration to change and these are policy. `AIProvidersService._validate_shape` raises each with its own message:

1. `kind = 'minted'` requires the adapter's `supports_minting`. Only OpenAI qualifies today; for every other type `fixed_key` is the normal path, not a degraded one.
2. `kind = 'minted'` requires `config.project_id` — there is no project to mint into and therefore no spend limit to verify.
3. `kind = 'fixed_key'` requires `base_url` and `model` where the adapter requires them. Delegated to `ai_credentials_service._validate_credential_data`, so the provider and the per-user pipeline share one rule rather than two that drift.
4. A type with no adapter at all is a `400`.

**The 1:1 with `managed_ai_credential` is not a schema constraint** — the FK allows many. It is established by `c23d6b59a8f5` (which asserts it as a post-condition) and held afterwards by `AIProvidersService.create`, which writes the pair in one transaction: one `flush` to order the inserts (nothing declares a relationship, so the unit of work otherwise emitted the child first and tripped the FK), then one `commit`. Two commits made the invariant breakable by the method that exists to hold it. A *second* credential on one provider is no longer expressible either: `ManagedAICredentialCreate` carries no `provider_id` field and forbids unknown keys.

A **lone** provider — one owning no credential — was creatable by `POST /admin/provider-admin-credentials`, which is deleted. `AIProviderPublic.owned_credential_id` stays nullable for rows that predate that deletion, because a projection that could not describe one would take the whole admin listing down over a row somebody has to look at to fix.

#### The dual-meaning secret column

`encrypted_secret` holds two categorically different things, and `kind` says which:

- **`fixed_key` — an ordinary model API key**, stored in the *same envelope a credential uses* (`{api_key, base_url?, model?}`, Fernet-encrypted), because each member's child `AICredential` is written from it verbatim. This is the concrete shape behind the prose, and it is the thing a future reader will get wrong: it is **not** a bare key string.
- **`minted` — an organisation administration secret**, stored bare. It never calls a model; it creates and destroys keys for a whole organisation, and it is the only means of revoking what it has minted.

Everything that touches the column branches on `kind` first. `rotate_key` refuses a `minted` provider and `fixed_key_envelope` answers `None` for one — both refusals are behaviour rather than defensiveness. Silently accepting a rotation that rotates nothing is how an admin comes to believe they rolled a key they did not, and handing an administration secret back as a model key would put it on a member's credential row and from there into the desktop client.

A `base_url` / `model` edit on a `fixed_key` provider **re-encrypts the envelope**, which is what makes the change reach members added afterwards as well as those already there. `_add_child` builds each new member's credential straight out of that envelope, so an edit that only diffed the credential's columns left members added before and after holding different shapes.

**Why a separate table.** Unchanged by the `fixed_key` addition, because the argument is about the *table*, not about which of the two secrets a given row holds. `ai_credential` is plumbing, and all of that plumbing would reach any secret the day it lived there: `model_discovery_service` selects *every* `ai_credential` row on a cron and calls the vendor with each key, and `external_account_config_service` hands *every* row a user owns, decrypted, to the desktop client. Neither has a bug; they are correct for the table they read. The isolation is structural — sharing (`ai_credential_share.ai_credential_id`), environment linking (`agent_environment.{conversation,building}_ai_credential_id`), bundle publisher wiring and the blast-radius counts are all foreign keys pinned to `ai_credential.id`; the environment credential bag is a fixed slot dict with no slot to pour into; and nothing iterates `SQLModel.metadata`.

`tests/architecture/ai_provider_isolation_test.py` asserts that no module in `app/` outside `ai_providers_service.py` and `key_provisioning_service.py` queries `AIProvider`. It has **no exception list**: the `PENDING` table it used to carry and the `xfail(strict=True)` twin that held the exception-free end state are both deleted, which is what they were built to make happen. A module that learns to query the table fails that test on the commit that teaches it.

### `managed_ai_credential_membership` — one column added (`45938a69aee7`)

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `claim_held_default_slots` | `BOOLEAN` | NOT NULL, server_default false | Whether the grant that created this row may take an SDK default slot its owner already occupies |

The column exists because the answer is needed at a different time from when it is known. A fixed-key member's defaults are wired **inside** the grant, with the caller's intent still on the stack. A **minted** member's are wired later, by a converge pass that cannot tell whether a superuser pressed "apply to existing users" or an account simply signed up. With nowhere to record it, that pass has to guess, and either guess is wrong for half its callers: automatic provisioning would steal defaults it must never take, or the two deliberate escape hatches would stop overwriting for exactly the provider kind the per-user-keys configuration is built on.

`false` for every existing row, which is both the safe direction and the correct one — nothing already granted should retroactively acquire permission to displace a default somebody chose.

### The two migrations, as a chain

`ed8d6a23f13c` → **`c23d6b59a8f5`** → **`45938a69aee7`**. They are one change in two steps and must be read in that order: the second adds a column to a table the first re-points.

#### `c23d6b59a8f5` — the split

**Upgrade**

1. `ALTER TABLE provider_admin_credential RENAME TO ai_provider`, with its index and FK constraints renamed to match.
2. Add the new columns with server defaults. `kind` gets `DEFAULT 'minted'`, because every row that predates this migration is an organisation administration secret.
3. `ALTER TABLE managed_ai_credential RENAME COLUMN provider_admin_credential_id TO provider_id`; drop and recreate the FK as **ON DELETE RESTRICT**; rename its index.
4. **Fail loudly, before the backfill moves a single row.** Five non-overlapping shapes abort the migration, each naming the offending id. An abort that has already half-moved a company key is worse than one that has not, and a row this migration cannot classify is a row whose key stops being handed out — which nobody notices on the day, only weeks later when a new hire has no key.

   | Shape | Why it cannot be classified |
   |---|---|
   | `auto_provision_roles` that is not a JSON array | Checked **first**, because every later check calls `json_array_length` on it and would otherwise surface a bare Postgres type error with no id in it |
   | A `shared` credential with non-empty roles but `encrypted_data IS NULL` | It auto-provisions by role and holds no key to build a provider from |
   | A `minted` credential with `provider_id IS NULL` | Nothing to mint through |
   | A `shared` credential with `provider_id IS NOT NULL` | Its pointer says "mint" while its mode says "copy one key"; either reading rewrites what its members hold |
   | A provider, or an auto-provisioning credential, whose type resolves to no adapter | Checked against the **adapter registry** rather than a type list written into the migration, so the check cannot drift from the adapters as they change |

5. **Backfill, in this order** (later steps rely on earlier ones):
   - **a.** For each existing `ai_provider` row (all `minted`), count the managed credentials pointing at it. `0` → create an empty minted managed credential, so the 1:1 invariant holds. `1` → nothing to do. `>1` → **split**: keep the first, and for each additional credential insert a new provider row copying the name (suffixed with the credential's name), type, secret and config, and re-point that credential. This duplicates the administration secret, which is the accepted consequence of storing it on the provider.
   - **b.** For each `managed_ai_credential` with `provider_id IS NULL` and non-empty `auto_provision_roles`: insert a `fixed_key` provider carrying that record's name, type, key, `base_url`, `model` and **all** of its wiring policy; set the credential's `provider_id`; NULL its `encrypted_data`. **The key bytes are moved unchanged**, which means a `fixed_key` provider created by this migration carries the `ai_credential.encrypted_data` codec — Fernet over `{api_key, base_url?, model?}` — not a bare key string. Re-encrypting here would mean decrypting every company key inside a schema migration. Whatever writes a `fixed_key` secret afterwards has to use that same envelope.
   - **c.** Copy each minted credential's wiring policy up onto its provider (step a has already established the pairing).
6. Drop `managed_ai_credential.auto_provision_roles` and `managed_ai_credential.provisioning_mode`, then assert the 1:1 invariant as a post-condition.

**Downgrade** reverses the renames, re-adds the dropped columns, copies the policy back down from the provider onto its credential, restores `encrypted_data` from the provider for `fixed_key` rows, and drops the providers steps 4a/4b created. Three things it states rather than guesses:

- **Split copies are not merged back.** There is no record of which row was the original, and merging would delete an administration secret that is the only means of revoking keys minted through it.
- A `fixed_key` provider's `last_verified_at`, `last_verify_error` and `config` are **dropped** when it folds back into its credential. The old schema has nowhere to put them; the key, the rule and the policy all survive.
- A `fixed_key` provider with an *empty* `auto_provision_roles` cannot have been created by this migration (step 4b only ever built one from a credential that had roles), so it postdates the upgrade. If its credential has members, the downgrade **refuses** rather than folding a configuration it has no record of into a manual credential behind the admin's back.

`backend/tests/migrations/ai_provider_split_test.py` seeds each abort shape and asserts the upgrade stops naming it, runs the round trip over the five shapes the plan enumerates, and pins the downgrade refusal and the RESTRICT direction.

#### `45938a69aee7` — the intent that has to outlive the request

Adds `managed_ai_credential_membership.claim_held_default_slots`, described above. It is a separate revision rather than part of the split because it is a separate discovery: `claim_held_slots` never reached `materialise_minted_child`, so for every minted provider "apply to existing users" silently stopped overwriting.

## Parent DTOs

### `ManagedAICredentialCreate`

| Field | Type | Notes |
|-------|------|-------|
| `name` | `str` | 1–255 chars |
| `type` | `AICredentialType` | Provider type; immutable after creation |
| `api_key` | `str \| None` | Plaintext key (min length 1 when present); encrypted into the record; written to children at add time. **Required** — a manual record is always `shared`, and a shared record with no key has nothing to give anyone. Nullable rather than required-with-a-sentinel so the omission is representable in the request model itself; the *service* raises the 400, because that rule is a policy and policies live in one place |
| `base_url` | `str \| None` | Required for `openai_compatible`; optional for `google`; max 500 chars |
| `model` | `str \| None` | Required for `openai_compatible`; max 255 chars |
| `default_model` | `str \| None` | Admin-curated default model (bare concrete id, max 255); normalized server-side (strip `provider/` prefix, trim) |
| `available_models` | `list[str] \| None` | Admin-curated selectable model list; normalized server-side; `None` = no curation |
| `expiry_notification_date` | `datetime \| None` | Informational expiry reminder |
| `target_user_ids` | `list[uuid.UUID]` | **May be empty** (phase 2 dropped the `min_length=1`): an auto-provision-only credential legitimately starts with no members. Deduplicated preserving order |
| `set_as_default` | `bool` | Default `False` |
| `set_user_sdk_defaults` | `bool` | Default `False` |
| `sdk_default_modes` | `list[str]` | Default `["conversation", "building"]`. **Not validated** — see Known Gaps in the [business doc](admin_ai_credential_provisioning.md#known-gaps) |
| `model_override_conversation` | `str \| None` | Max 255; normalized through `_normalize_default_model` (trim, strip `provider/`, blank → `None`) |
| `model_override_building` | `str \| None` | Same |

**Manual records only, and the three retired fields are refused rather than ignored.** `auto_provision_roles`, `provisioning_mode` and `provider_admin_credential_id` are gone from this model, and the model sets `extra="forbid"`, so a client still sending one gets a `422` naming the field instead of a `200` that changes nothing. Removing a field from a Pydantic model does not refuse it, it ignores it — and the ignored case had a real cost: `auto_provision_roles: []` on a provider-owned record used to be accepted, write nothing anywhere, and leave the admin believing they had stopped that provider auto-provisioning.

A provider-owned record is created by creating its provider. There is no `provider_id` field here, which is what makes a second credential on one provider inexpressible rather than merely refused.

### `ManagedAICredentialUpdate`

All fields optional (partial update). Omitting `api_key` keeps the stored key. Omitting `target_user_ids` leaves membership unchanged. Omitting `available_models` leaves the stored curation unchanged; sending `[]` clears it.

| Field | Type | Notes |
|-------|------|-------|
| `name` | `str \| None` | |
| `api_key` | `str \| None` | Non-None triggers key rotation + Update pass for all current members. **On a provider-owned record it is a `400` naming the provider.** Accepting it would write `encrypted_data` on a record whose key the provider owns, and the next `AIProvidersService.rotate_key` would re-key every member from that stale copy while reporting success — the rotation refusal defeated through the other door |
| `base_url` | `str \| None` | |
| `model` | `str \| None` | |
| `default_model` | `str \| None` | `None` = no change; blank string normalized to `None` (effectively clears). Normalized server-side. |
| `available_models` | `list[str] \| None` | `None` = no change; `[]` = explicit clear (fall back to `discovered_models`). Normalized server-side. |
| `expiry_notification_date` | `datetime \| None` | |
| `target_user_ids` | `list[uuid.UUID] \| None` | When supplied, the reconcile diff is against this list |
| `set_as_default` | `bool \| None` | |
| `set_user_sdk_defaults` | `bool \| None` | |
| `sdk_default_modes` | `list[str] \| None` | |
| `model_override_conversation` | `str \| None` | Three-valued: omitted/`null` = unchanged; `""` = clear to NULL **and** retract the pin from members still carrying the dropped value; a model id = set + write through. This is why the field is not `str \| None` with `min_length=1` — the empty string is meaningful here, not malformed |
| `model_override_building` | `str \| None` | Same |

**`extra="forbid"` here too**, and on a provider-owned record `_refuse_shadowed_writes` refuses more than the shadowed set. Every submitted offender is named in one `400`, not one per round trip:

| Refused on a provider-owned record | Why |
|---|---|
| `set_as_default`, `set_user_sdk_defaults`, `sdk_default_modes`, `default_model`, `available_models`, `model_override_conversation`, `model_override_building`, `expiry_notification_date` | `ManagedAICredentialsService.SHADOWED_FIELDS` — the §3.2 set, pinned field-for-field by `tests/architecture/managed_credential_shadowed_fields_test.py`. Writing one here would save successfully and change nothing |
| `api_key` | The key itself; see the row above |
| `base_url`, `model` | The key's **shape**. For a `fixed_key` provider they live in the provider's encrypted envelope, which `_add_child` builds each new member's credential from. Accepting one wrote through to today's members and was silently reverted for every member added afterwards, and by the next `rotate_key` — two members of one credential holding different base URLs, with nothing reporting the divergence |

The last three are added to the refusal's own local tuple rather than to `SHADOWED_FIELDS`, because that constant is the shadowed-*policy* set the architecture test pins.

### `ManagedAICredentialPublic`

Never includes `encrypted_data` or key material.

| Field | Type | Notes |
|-------|------|-------|
| `id` | `uuid.UUID` | |
| `name` | `str` | |
| `type` | `AICredentialType` | |
| `base_url` | `str \| None` | |
| `model` | `str \| None` | |
| `default_model` | `str \| None` | Curated default model (bare concrete id) |
| `available_models` | `list[str] \| None` | Curated selectable model list |
| `set_as_default` | `bool` | |
| `set_user_sdk_defaults` | `bool` | |
| `sdk_default_modes` | `list[str]` | |
| `model_override_conversation` | `str \| None` | |
| `model_override_building` | `str \| None` | |
| `expiry_notification_date` | `datetime \| None` | |
| `managed_by_id` | `uuid.UUID \| None` | Which admin manages this; NULL when that admin was deleted |
| `provisioning_mode` | `ProvisioningMode` | **Required, no default, and computed** through the policy resolver rather than stored. An absent mode rendered through a client-side `!== "minted"` fallback is the browser deciding a record holds one shared key because a field did not arrive |
| `provider_id` | `uuid.UUID \| None` | The provider that owns this record; `None` for a manual one. Renamed from `provider_admin_credential_id` when the wire moved to the provider vocabulary — the administration secret is one of the two things a provider's secret can be, not the name of the relationship |
| `provider_name` | `str \| None` | Resolved once by the batch policy resolver, so the credentials table can render a **Source** column without a request per row |
| `is_provider_owned` | `bool` | The **answer** to "is every wiring control read-only here", rather than `provider_id !== null` re-derived in the browser. The two agree today; only one of them keeps agreeing the day the rule changes |
| `has_api_key` | `bool` | Whether a key can be resolved for this record. **Computed**, not the constant it used to be: `False` for a minted record, which has no shared key by design rather than a missing one |
| `is_oauth_token` | `bool` | Derived from the stored key prefix (`sk-ant-oat`) for Anthropic; `False` for all other types |
| `members` | `list[ManagedAICredentialMember]` | One entry per **membership row**, not per child credential |
| `member_count` | `int` | `len(members)` |
| `created_at` | `datetime` | |
| `updated_at` | `datetime` | |

`auto_provision_roles` is **not** published here, and not in the generated client either — the column is gone from the table and the rule is a property of a provider, published on `AIProviderPublic`. Restating it would put a second answer on the wire for a question that has one owner.

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

### `AIProvider*` DTOs

The wire vocabulary: the entity is a **Provider**, and `type` is the vendor. The stored column is still `provider_type`, so the projection does the translation once here rather than in every caller.

| DTO | Notes |
|-----|-------|
| `AIProviderConfig` | An alias for `ProviderAdminCredentialConfig`, the stored shape: `organization_id?`, `project_id?`. **No spend-limit field:** the limit is set on the vendor's console and only ever read here, so there is no local copy to go stale. The project is still verified to carry a hard limit before the first key is minted |
| `AIProviderConfigInput` | The **request-side** config: same fields, `extra="forbid"`. A subclass rather than forbidding extras on the parent, because the parent is also the response shape — forbidding there would make a stored config carrying a stray key raise on *read*, taking the admin listing down over a row somebody has to look at to fix. Strict going in, tolerant coming out. The reason it is strict is specific: `organization_id` is spelled the American way while this codebase's prose spells it the British way throughout, so `organisation_id` is the likeliest typo on the surface — and it lands on one of the two fields that select which project keys are minted into. Dropped silently, the provider verifies as uncapped and refuses to mint with nothing saying a field was thrown away |
| `AIProviderCreate` | `name`, `kind`, `type`, `secret`, `config`, `base_url?`, `model?`, the rule, the whole wiring policy, and `target_user_ids` (may be empty — an auto-provision-only provider legitimately starts with nobody). **`kind` has no default**, for the same reason the column has none in Python: the secret means two categorically different things depending on it, and a caller that did not say which one it is pasting has not been asked the question. **`extra="forbid"`**, because there is exactly one secret field and for a `minted` provider it *is* the administration key — a body also carrying `api_key`, the spelling every other credential surface uses, would otherwise be accepted, ignored, and leave an admin believing they had given the provider a member key |
| `AIProviderUpdate` | Partial; every omission means "leave it alone". `kind`, `type`, `secret` and `target_user_ids` are **absent on purpose**: the first two would re-point an existing member's key at a different vendor or change what the stored secret means; `secret` is absent because replacing a `fixed_key` provider's key is `POST /{id}/rotate-key`, and a `secret` quietly ignored here would look exactly like the rotation that endpoint exists to perform; membership is edited on the credential the provider owns. `model_override_*` keeps the three-state contract of `ManagedAICredentialUpdate` **verbatim**, because the same members are on the other end of it. `extra="forbid"` |
| `AIProviderPublic` | `id`, `name`, `kind`, `type`, `config`, **`has_secret: bool`** (never the value; there is no reveal endpoint), `base_url`, `model`, the rule, the whole wiring policy, `last_verified_at`, `last_verify_error`, `created_by_id`, `owned_credential_id`, `member_count`, `key_state_summary`, timestamps. `key_state_summary` is `membership status -> count` for the owned credential's members — the admin list's "is anything stuck" answer, computed server-side so two surfaces cannot disagree about which statuses mean "has a key" |
| `AIProviderRotateKey` | `{api_key}`. `fixed_key` only |
| `AIProviderVerifyResult` | `ok`, `account_ref` (vendor-side identity — never key material), `checked_spend_limit`, `spend_limit_enforcing`, `spend_limit_cents`, `error`. **`checked_spend_limit` is the field that keeps the answer honest**: the spend fields are only answered for a `minted` provider, and reporting `False` on a `fixed_key` one would read as "no cap configured" rather than "the question does not apply" |
| `AIProviderDeleteMember` | `user_id`, `email`, `full_name?`, `holds_provider_key` — whether a key minted at the vendor exists for this member and will be revoked there |
| `AIProviderDeleteImpact` | `provider_id`, `provider_name`, `owned_credential_id?`, `member_count`, `minted_key_count`, `members`. **Named people rather than a bare count**, because "3 users will lose a key" is not something an administrator can check before pressing |

The four `ProviderAdminCredential*` request/response DTOs are **retired** with the route that used them. `ProviderAdminCredentialConfig` is not retired — it is the stored config shape, aliased as `AIProviderConfig`.

### `ProviderAdapterPublic` / `ProviderAdaptersPublic`

Documented with the registry in [provider_adapters_tech](provider_adapters_tech.md#get-apiv1adminai-providersadapters). An adapter describes a **type**, never a Provider; that file states the rule and the projection's `by_type` equality test enforces it.

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

### AI Providers (`/api/v1/admin/ai-providers`)

**File:** `backend/app/api/routes/admin_ai_providers.py` · **Tag:** `admin-ai-providers` · **Auth gate:** `get_current_active_superuser` on every endpoint, including `/adapters` · **The secret is write-only** — accepted on create, replaced by `rotate-key`, in no response.

| Method | Path | Request | Response | Notes |
|--------|------|---------|----------|-------|
| `GET` | `/admin/ai-providers/adapters` | — | `ProviderAdaptersPublic` (`{data, count}`) | Every **type** the server supports. Declared above `/{provider_id}` so the literal wins the match. Full field list in [provider_adapters_tech](provider_adapters_tech.md#get-apiv1adminai-providersadapters) |
| `POST` | `/admin/ai-providers/` | `AIProviderCreate` | `AIProviderPublic` | Creates the provider **and its one managed credential** in one transaction, then reconciles the initial members. `400` on an incoherent kind/type/shape; `409` on a newly claimed `(role, mode)` slot; `422` on an unknown key |
| `GET` | `/admin/ai-providers/` | — | `list[AIProviderPublic]` | Bare array |
| `GET` | `/admin/ai-providers/{provider_id}` | — | `AIProviderPublic` | `404` if not found |
| `PATCH` | `/admin/ai-providers/{provider_id}` | `AIProviderUpdate` | `AIProviderPublic` | Re-applies the policy to every existing member. `409` on a newly claimed slot |
| `DELETE` | `/admin/ai-providers/{provider_id}?force=` | — | `Message` | Two distinct `409` shapes, both carrying a `code` — see below |
| `POST` | `/admin/ai-providers/{provider_id}/verify` | — | `AIProviderVerifyResult` | Two different questions by `kind`; the result says which was asked |
| `POST` | `/admin/ai-providers/{provider_id}/rotate-key` | `AIProviderRotateKey` | `AIProviderPublic` | `fixed_key` only; `400` on `minted` |
| `POST` | `/admin/ai-providers/{provider_id}/apply-to-existing?dry_run=` | — | `ManagedAICredentialApplyResult` | Add-only grant to every active account whose role the provider covers, minus current members. A dry run writes nothing and emits no audit event. The response is the credential's *apply* result (a subclass of the reconcile result), because narrowing it would drop the `candidates` / `defaults_overwrite_count` fields the confirm dialog's preview is built from |

**The `(role, mode)` conflict envelope** moved routers verbatim. `admin_ai_providers._conflict_409` is the live one, and it keeps the `conflicting_credential_id` / `conflicting_credential_name` key names because a dialog parses them:

```json
{"code": "auto_provision_conflict",
 "message": "'Company Claude' already sets the building default for auto-provisioned agent-developer accounts.",
 "conflicting_credential_id": "…", "conflicting_credential_name": "Company Claude",
 "role": "agent-developer", "mode": "building"}
```

**`DELETE` answers two different 409s, and both carry `code`.** One endpoint answering two `409` shapes, only one of which carries `code`, hands a client `undefined` half the time.

| `detail.code` | When | Body |
|---|---|---|
| `ai_provider_in_use` | Unforced, and anybody holds a key | `{code, message, impact}` where `impact` is the full `AIProviderDeleteImpact` — **named people**, not a count |
| `ai_provider_members_not_removed` | Forced, the revoke and the row removals ran, and the **credential row survived** | `{code, message, blocked}`. The admin retries; the retry completes the removal |

**Structural router tests.** `tests/api/ai_credentials/admin_ai_providers_test.py` asserts that every route in the module declares a `response_model`, and that every declared model is in a reviewed set of secret-free projections — enumerated over `router.routes`, so an endpoint cannot be added without somebody deciding in writing that its shape is safe. The secret-absence assertion runs against the **raw key set** of the response, so a key the response model never declared is visible rather than filtered out of the check.

### Admin LLM Providers (`/api/v1/admin/llm-providers`)

**File:** `backend/app/api/routes/admin_llm_providers.py` · **Tag:** `admin-llm-providers` (path and tag deliberately unchanged, so the Managed Credentials surface does not churn) · **Auth gate:** `get_current_active_superuser` — `403` for anyone else

| Method | Path | Request | Response | Notes |
|--------|------|---------|----------|-------|
| `POST` | `/admin/llm-providers/` | `ManagedAICredentialCreate` | `ManagedAICredentialReconcileResult` | Creates a **manual** record + initial reconcile. `400` from `_validate_provisioning_shape` when no key was supplied; `422` naming the field for any of the three retired ones |
| `GET` | `/admin/llm-providers/` | query `?managed_by_id=` & `?target_user_id=` (both optional) | `list[ManagedAICredentialPublic]` | Fleet-wide, ordered by `created_at DESC`; filtered when params supplied |
| `GET` | `/admin/llm-providers/{id}` | — | `ManagedAICredentialPublic` | `404` if not found |
| `PATCH` | `/admin/llm-providers/{id}?force=` | `ManagedAICredentialUpdate` | `ManagedAICredentialReconcileResult` | Update + re-reconcile; `force` overrides the Tier-2 block on removed members. **`400` naming the provider** for any shadowed field, `api_key`, `base_url` or `model` on a provider-owned record |
| `DELETE` | `/admin/llm-providers/{id}?force=` | — | `Message` | `409` with a `blocked` list when any child is in use and `force` is absent. **`400` naming the provider** for a provider-owned record — deleting it would leave the provider auto-provisioning with nothing to grant through, and the scan drops such a provider silently |
| `POST` | `/admin/llm-providers/{id}/set-default` | — | `ManagedAICredentialPublic` | Sets every current member's child as their default. One of the two deliberate acts that **do** take a held slot |
| `POST` | `/admin/llm-providers/{id}/members/{user_id}/retry` | — | `ManagedAICredentialPublic` | Requeue a terminally `failed` member. `400` if their provisioning has not failed; `404` if they are not a member. Emits `admin.ai_credential.mint_requested` carrying `retry_of: <last_error>` |
| `POST` | `/admin/llm-providers/test-connection?managed_credential_id=` | `AICredentialTestRequest` | `AICredentialTestResult` | When `api_key` is blank and `managed_credential_id` is given, probes via the resolved key — which for a provider-owned record is the **provider's**, not a stale copy on the credential row |

**`POST /admin/llm-providers/{id}/apply-to-existing` is removed.** For a provider-owned record it ran the identical `ManagedAICredentialsService.apply_to_existing` call the provider route runs, and for a manual record — which can never carry `auto_provision_roles` — it always answered `candidate_count: 0`. It was cut rather than kept as an honest no-op, on the precedent `rotate-key` sets: rotating a `minted` provider is a `400`, not an accepted rotation that rotates nothing. `ManagedAICredentialsService.apply_to_existing` is unaffected; `AIProvidersService.apply_to_existing` delegates to it, and it remains the reconcile either route ever ran.

### Owner-facing provisioning list (`/api/v1/ai-credentials/provisioning`)

| Method | Path | Response | Notes |
|--------|------|----------|-------|
| `GET` | `/ai-credentials/provisioning` | `list[UserKeyProvisioningPublic]` | `CurrentUser`; the caller's own keyless memberships (`pending`, `minting`, `failed`). **Declared above `/{credential_id}`** so the literal path is matched before the UUID route claims it |

> **List-envelope inconsistency.** `GET /admin/ai-providers/adapters` returns `{data, count}`; `GET /admin/ai-providers/`, `GET /admin/llm-providers/` and `GET /ai-credentials/provisioning` return bare arrays. Each is consumed correctly by the client that reads it, but "how does an admin list respond" has two answers. Recorded in the business doc's [Known Gaps](admin_ai_credential_provisioning.md#known-gaps).

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

### Auto-provision uniqueness — moved to `AIProvidersService`

`_claimed_slots` and `_validate_auto_provision_uniqueness` no longer live here. The rule they enforce — one owner per `(role, mode)` default slot — is **provider vs provider** now, because the rule that claims a slot lives on `ai_provider` and a managed credential has no auto-provision roles of its own to fight with. The claim itself is computed by `ProvisioningPolicy.claimed_slots`, so the write-time check and the run-time grant read one definition. See [The two conflict rules](#the-two-conflict-rules-at-two-different-times).

### `_refuse_shadowed_writes` and the provider-owned delete refusal

Two edges on this service that exist because the credential surface can otherwise reach around the provider.

`_refuse_shadowed_writes(data, policy)` names **every** submitted offender in one `400` — an admin who sent three should not discover them one round trip at a time — and the sentence points at the fix: *"… are managed by the AI provider 'X'. Edit the provider instead; changing it there re-applies to every member."* The field list is `SHADOWED_FIELDS` plus `api_key`, `base_url` and `model`; the reasons for those three are in the [`ManagedAICredentialUpdate`](#managedaicredentialupdate) table above.

`delete(..., allow_provider_owned=False)` refuses a provider-owned record with a `400` naming the provider, and the reason is sharper than symmetry. The FK is `ON DELETE RESTRICT` in the other direction only, so deleting the credential leaves the `ai_provider` row standing with its `auto_provision_roles` still populated — and `AIProvidersService.auto_provision_targets` drops a provider that owns no credential **silently**: no skip, no log, no event. Every subsequent signup for those roles would get nothing, for ever, while the admin surface went on showing the rule as active.

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
| `on_account_created(session, user, origin) -> ProvisioningReport` | Grants every **provider** whose `auto_provision_roles` contains `user.role`, through the credential that provider owns. **Never raises**, unconditionally — including when handed a session whose transaction is already aborted. Returns a value, never an error; its production caller ignores it, which is what lets tests assert on failures without the production path branching on one |
| `provision_explicit(session, user, origin, *, provider_ids, actor) -> ProvisioningReport` | The invitation wizard's entry point, and the only sanctioned way an explicit list is granted at account creation. Same net, same body, same report as the automatic path — `_guarded` and `_provision` are shared, so the two cannot drift. `provider_ids=None` selects the automatic set by the same predicate; `[]` grants nothing; a list grants exactly those and **not** the automatic set as well. `actor` is the acting superuser and is **required** (keyword-only, no default), and reaches both `add_members` and `details.actor` |
| `_guarded(session, user, origin, run, *, label)` | The outer never-fail net, factored out so the two entry points cannot each forget `_restore_session` or log `user.id` before the repair. `label` (`"automatic provisioning"` / the invite path's own) is required and keyword-only for the same reason `add_members`' `actor` is: with a default, the invite path would silently report itself as automatic provisioning and an admin debugging a failed invitation would grep for a string that is never written |
| `_provision(session, user, origin, *, provider_ids, actor, label)` | The shared body. `provider_ids is None` calls `AIProvidersService.auto_provision_targets`; a list calls `grant_targets`, deduplicated, with any id that no longer names a **grantable** provider reported as a `provider_not_found` skip rather than dropped. Everything after the selection — the per-target guard, the skip reporting, the audit events — is identical for both entry points. The inactive short-circuit runs *before* the selection and is the one place they differ: it reports one `user_inactive` skip per **requested** provider, which only the explicit path can name |
| `on_account_deactivated(session, user) -> None` | Deletes the user's minted child credentials, moves their memberships to `suspended`, and hands the provider revokes to the background loop. **Shared credentials are untouched** — one key held by many people must not be destroyed because one holder left. Synchronous like both of its callers: the database work happens inline (so nothing downstream can observe an account that is deactivated but still holds a live key row) and only the provider call is deferred. Wrapped in its own never-fail net — deactivating an account must not fail because a provider record could not be tidied |
| `on_account_reactivated(session, user) -> None` | The mirror. Moves `suspended` memberships back to `pending` at zero attempts; a **fresh** key is minted. It exists because the membership survived the deactivation — without it a reactivated employee would be a member with a `suspended` row nothing ever picks up |
| `on_account_deleted(session, user, *, actor_id) -> list[RevocationRequest]` | **Call before the delete, schedule after it.** Both deletion routes are a bare `session.delete(user)`, so the membership rows and their provider handles are gone the moment it commits. Returns the requests rather than scheduling them, so the provider is only contacted if the deletion actually commits. `actor_id` names whose feed the revoke is recorded in — deliberately *not* the key holder, whose `user` row is about to be gone; `None` for self-deletion, where subject and actor are the same and both are being removed |

**The `is_active` call sites are two**, and the list is worth keeping accurate because the last audit of it found one missing: `api/routes/users.py::update_user` (on the transition) and `InvitationService._apply_reinvite_updates` (the re-invite path — it flips `is_active` on an *existing* account and is the caller a reader would not think to look for). **Deletion is explicitly not a third:** `delete_user` / `delete_user_me` call `on_account_deleted` instead, because a deleted account's rows are gone by the time anything could read them.

**`AccountProvisioningService` does not query `ai_provider`.** It asks `AIProvidersService.auto_provision_targets(session, role)` (the role predicate) or `grant_targets(session, provider_ids)` (an explicit list), each returning `ProviderGrantTarget` — a `(provider, its managed credential)` pair. That is what lets the isolation invariant hold with the account-creation path still auto-provisioning, and it is why this module came off the isolation test's exception list rather than staying on it. A provider that owns no credential yields **no target**, and read through the explicit path it reads back as `provider_not_found`.

Result dataclasses:

- `ProvisioningReport(added, skipped, default_slot_skips, failed)`
- `ProvisionedCredential(provider_id, managed_credential_id, child_credential_id)` — **both ids**, because they answer different questions: `provider_id` is what the request named and what a skip is keyed by, so a caller can line the two lists up; `managed_credential_id` is where the membership actually landed. `child_credential_id` is `None` for a membership of a minted record — the person is a member from this moment, and their key is created out of band a little later
- `ProvisioningSkip(provider_id, reason)` — keyed by the **provider**, because that is what a caller named. `reason` is a stable machine string: the reconcile skip reasons (`user_not_found`, `user_inactive`, `provision_failed`), `add_members_failed` when the call itself raised, or `provider_not_found`, which only the explicit-list path can produce and is therefore the one most likely to be missing from a hand-written frontend map. `managed_credential_not_found` is **gone** — nothing produces it
- `ProvisioningDefaultSlotSkip(provider_id, mode)` — **disclosure, not a failure.** A skip means the person did *not* receive a key; this means they did, and the one thing that did not happen is their `default_sdk_<mode>` being repointed at it. Folded into `skipped` it would render as a grant that failed, which is the opposite of what happened, and it leaves `provisioning_failed` untouched. It rides up from `MemberAddition.default_slot_skips` and surfaces as `InviteProvisioningSummary.default_slot_skips`. The credential already sitting in the slot is deliberately **not** carried up: it is an `ai_credential` id belonging to the invited person's own configuration, and the wizard has no name for it

The invite route projects this report into its own narrow `InviteProvisioningSummary(added_count, skipped: list[InviteProvisioningSkip], default_slot_skips: list[InviteProvisioningDefaultSlotSkip], provisioning_failed)` rather than returning `ManagedAICredentialReconcileResult`: that shape carries `removed`/`blocked`/`updated`, which an add-only grant can never populate, and a `record` projection whose construction costs a per-member user lookup and a key decrypt.

`provisioning_failed` carries `ProvisioningReport.failed`, which `_guarded` sets when the outer net fires. Without it a total failure — a raise in `_provision`'s prologue, before any credential has been selected — returns a report that is byte-identical to a deliberate grant of nothing (empty `added`, empty `skipped`), and the wizard renders both as "No AI credentials were granted". It is deliberately not a skip entry: a skip names a credential, and this failure can happen before any credential has been named. The two "nothing to do" returns inside `_provision` (an inactive account, an empty explicit id list) stay `failed=False` — they are outcomes, not failures. The inactive one is nevertheless not *empty*: it returns one `user_inactive` skip per requested provider, for the same reason `failed` exists, since an admin who ticked three credentials and invited a deactivated account would otherwise see exactly the screen of an admin who ticked none. That state is now reachable — re-inviting an account an administrator deliberately deactivated no longer reactivates it (`InviteUserRequest.is_active` is `bool | None`, and `None` means the submission did not state one). No security event is written for these skips: a medium-severity row per provider, in the feed of an account that has done nothing, is noise about an outcome that was never in doubt. The wizard renders them as one amber line, not N identical skip lines.

**Two error-handling rules, both non-obvious enough to state:**

- **Repair the session before touching it.** A failed attempt can leave the transaction aborted, and in that state *every* session operation raises — including the lazy attribute load behind an innocent-looking `logger.warning("… %s", user.id)`. So identifiers are snapshotted into locals while the session is known good, `_restore_session` runs first, and only then does anything get logged. The first parent in the loop hides this (`user` is still fresh from `create_account`'s refresh); it is the second, after a child credential has committed and expired everything, that bites.
- **Roll back unconditionally.** `AccountProvisioningService._restore_session` is a thin wrapper over the shared `restore_session(session)` in `backend/app/utils.py` (shared with `ManagedAICredentialsService`, the other end of this same path); it deliberately does **not** guard on `session.is_active`. That predicate detects only half the problem: a *flush* failure deactivates the `SessionTransaction`, but a *statement* failure (a `select` that errors, a lock timeout, a serialization failure, a dropped connection) leaves Postgres' transaction aborted while `is_active` stays `True` — so the guard would skip exactly the case that needs the rollback, and the caller's next commit (`register_user` commits again to send its confirmation email) dies with "current transaction is aborted". The rollback is safe by construction: the account row is committed before the call and `add_members` commits each child individually, so anything still pending belongs to the failed attempt. The helper lives in `app.utils` rather than on either service precisely because it is a pure session-lifecycle concern with no domain knowledge, and two copies is how one of them ends up guarded on `is_active` again. Whether rolling back is *safe* is left to each caller — `restore_session` never decides that, and never raises.

Other structural choices: the outer `try` in `on_account_created` wraps the prologue (deferred import, the target lookup, the role filter) that the per-target guards do not cover; `user_id` is pre-bound to `None` and snapshotted *inside* the `try` because reading `user.id` is itself a session operation. Providers are filtered in Python (the table holds a handful of rows, and a portable JSON-containment predicate over a `json` — not `jsonb` — column is more machinery than the saving is worth), with an `isinstance(…, list)` guard so a hand-edited non-list `auto_provision_roles` cannot raise on the signup path. Inactive accounts return an empty report before any provider is looked at — no children, no skips, no medium-severity events in the feed of an account nobody can sign into.

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

### What the provider's console shows

The name sent with the create is `"{owner.email} ({membership.id})"` — the member's email address, postfixed with the id of the membership row the mint was claimed off. It is read by a person standing in the provider's own UI asking whose key a service account is, so the email leads; the id is what makes two keys for the same person (a re-mint after a revoke, the same person under two providers) tellable apart, and it names the row whose `ai_credential_id` points at the credential the key ended up on.

**The credential's own id cannot go there.** It does not exist yet — the child row is created *from* the minted secret — and no provider offers a rename afterwards: OpenAI's administration API can create, list and delete a service account but not modify one. The membership id is the only identifier that exists on both sides of the mint.

That name is also the sole record of the one leak this design cannot close: the orphan a crash inside the mint window leaves is nameable in the console because it carries the member's email.

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

## The policy resolver (`provisioning_policy.py`)

`resolve_policy(session, parent) -> ProvisioningPolicy` is **the only legal way to read any of a managed credential's wiring facts**. `resolve_policies(session, parents)` is the batch form, fetching each provider once for a whole list page rather than once per record.

```
ProvisioningPolicy (frozen dataclass)
    source: "provider" | "manual"
    provider_id: UUID | None
    provider_name: str | None
    auto_provision_roles: list[str]
    set_as_default: bool
    set_user_sdk_defaults: bool
    sdk_default_modes: list[str]
    default_model: str | None
    available_models: list[str] | None
    model_override_conversation: str | None
    model_override_building: str | None
    expiry_notification_date: datetime | None
    provisioning_mode: ProvisioningMode
```

Frozen because it is an **answer, not a handle**: a caller that could mutate it would be editing a policy nothing persists, which reads as a write and is not one. Write to the provider (or, for a manual record, to the credential) and resolve again.

**What is deliberately not on it.** Not the key — a policy object is copied, logged and compared, and a secret has no business in one; `fixed_key` material is fetched separately, by the one service allowed to read the provider table, at the moment it is needed. Not `name` / `base_url` / `model` either: those stay the credential's own columns even for a provider-owned record, because they are the *shape of the child credentials it writes*, not the policy for wiring them.

`provider_name` is on it for one concrete reason — a refusal has to be able to say *which* provider the admin has to go and edit.

**`claimed_slots()`** is the `(role, mode)` set a policy lays claim to, and it is the same definition `AIProvidersService._claimed_slots` applies to a *prospective* configuration (the service overload exists because a create has no stored row to resolve from). Both filter modes to `POLICY_MODES`, so a typo in `sdk_default_modes` wires nothing and therefore collides with nothing. `tests/unit/test_provisioning_policy.py::test_the_services_prospective_claim_matches_the_resolved_one` asserts the two agree across seven configurations — two implementations of one rule, previously only claimed equal in prose.

**Three branches, and the third is the interesting one.** No `provider_id` → the credential's own columns. A provider that resolves → the provider's values. A `provider_id` that resolves to **nothing** → a provider-owned policy that wires nothing and auto-provisions to nobody. It deliberately does *not* fall back to the record's own shadowed columns: those hold whatever they held before the provider took ownership, and presenting stale values as the active policy is the failure this module exists to prevent. RESTRICT means no service path produces that row; the branch is there so a list page renders instead of raising.

**It does not query `ai_provider` itself.** It resolves providers *through* `AIProvidersService`, so the isolation allowlist stays at two files rather than growing one for the resolver. The import is function-local, because `ai_providers_service` imports `managed_ai_credentials_service`, which imports this module.

### The two architecture tests that make it enforceable

| Test | What it holds |
|---|---|
| `tests/architecture/managed_credential_shadowed_fields_test.py` | No module outside `provisioning_policy.py` **reads** `ManagedAICredential.{set_as_default, set_user_sdk_defaults, sdk_default_modes, default_model, available_models, model_override_*, expiry_notification_date}` directly. Reads only, not writes — a manual record's columns are still written, and the provider-owned path refuses those writes at the edge. The same file pins `ManagedAICredentialsService.SHADOWED_FIELDS` field-for-field, so the refusal's field list and the test's cannot drift apart |
| `tests/architecture/ai_provider_isolation_test.py` | No query in `app/` selects `AIProvider` outside `ai_providers_service.py` and `key_provisioning_service.py`. **No exception list at all** — the `PENDING` table and its `xfail(strict=True)` twin are both deleted |

**The first test under-approximates on purpose, and says so in its own docstring.** It is an AST inference, not a type checker: it sees receivers bound by a parameter annotation, an annotated assignment or a `session.get(ManagedAICredential, …)`, and a read off a receiver reached any other way is invisible to it. Green there means *no statically visible second reader*, which is a weaker claim than "no second reader" and is the one to rely on.

---

## `AIProvidersService` (`services/credentials/ai_providers_service.py`)

Singleton: `ai_providers_service`. Superuser-only. **The provider's secret leaves this module only as an argument to a vendor call, or as the envelope a child credential is written from.**

| Method | Notes |
|--------|-------|
| `create(session, data, actor)` | `_validate_shape` → `_validate_auto_provision_uniqueness` → encrypt → **provider + managed credential in one transaction** (one `flush` to order the inserts, one `commit`) → reconcile the initial members. Validating before the insert is what stops a conflict leaving a half-configured provider behind |
| `update(session, provider_id, data, actor)` | Snapshots `previously_claimed` and the previous overrides **before writing a single field** — both are transitions and neither is recoverable once the new values are in — validates against the *effective* values, writes, re-encrypts the `fixed_key` envelope on a `base_url`/`model` edit, then `_reapply_to_members` |
| `rotate_key(session, provider_id, api_key, actor)` | `fixed_key` only. Re-encrypts on the provider **and writes the new key through to every member's child**. `400` on `minted` |
| `verify(session, provider_id)` | `minted`: administration access + the spend-limit read. `fixed_key`: the type's test-connection call. Stamps `last_verified_at` / `last_verify_error`. `checked_spend_limit` says which question was asked |
| `apply_to_existing(session, provider_id, actor, *, dry_run)` | Delegates to `ManagedAICredentialsService.apply_to_existing` with `claim_held_slots=True` — this is one of the two deliberate acts that overwrite |
| `delete_impact(session, provider)` | The `409` body, as named people |
| `async delete(session, provider_id, actor, *, force)` | The gate — see below |
| `auto_provision_targets(session, role)` / `grant_targets(session, provider_ids)` | `(provider, its managed credential)` pairs. The rule lives on the provider and the members live on the credential, so a grant needs both — and a caller that is not allowed to query `ai_provider` cannot pair them itself. This is what keeps `AccountProvisioningService` off the isolation exception list |
| `decrypt_secret(record)` / `fixed_key_envelope(record)` | The two reads of the secret, branched on `kind`. `fixed_key_envelope` answers `None` for a `minted` provider rather than handing an administration secret back as a model key |
| `write_policy_fields(session, provider_id, fields)` | A narrow write for a path on the *credential* side holding a value that belongs to the provider — writing it onto the shadowed columns would save successfully and change nothing. Takes already-normalised values, does no validation of its own, and does **not** re-apply to members; a caller that needs that must reconcile the owned credential itself |
| `list` / `get` / `to_public` | Projection with `owned_credential_id`, `member_count`, `key_state_summary` |

### `_reapply_to_members` — why a policy edit is not just a column write

The machinery already existed; the provider had to be able to reach it. `update` calls into `ManagedAICredentialsService.reconcile` / `_update_child_fields` / `_sync_model_overrides` for the owned credential, carrying `cleared_overrides` down so a cleared override actually **retracts** members' pins rather than being cosmetic. `default_model` and `available_models` are written through to every child row; `model_override_*` is written through to every member whose default still points at their child.

A **null** reconcile result is not an error. It means the provider owns no managed credential — a *lone* row, only ever creatable by the deleted route — so there is nothing to re-apply to and nobody to tell. The response is the provider projection either way and the per-member events are simply not written. Refusing instead would make a legacy row un-renameable.

### The delete gate, and why the revoke comes **first**

1. Unforced, and the owned credential has **any** membership row → `AIProviderInUseError` carrying the impact → `409`.
2. Forced: **await the revocations**, then `ManagedAICredentialsService.delete(force=True, allow_provider_owned=True)` (children, memberships, the credential), then the provider row.
3. A provider whose credential has no members deletes without a confirmation.

**Step 2's ordering inverts this service's usual delete-then-revoke rule, and that is the point.** Everywhere else the revoke comes second, because the delete can be refused by the blast-radius gate and destroying a key for a removal the database then declines is not recoverable. Here the thing being deleted **is the secret the revoke needs**: a *scheduled* revocation would run after `session.delete(provider)` had committed, find no administration secret, and record every key as `revoke_failed` — live at the vendor forever with only an audit line naming it. So `revoke_now` is awaited (which is why `delete` is `async`) while the secret still exists, and the handles are cleared from the rows afterwards so the credential delete cannot schedule a second, doomed attempt at the same key.

`allow_provider_owned` is the one sanctioned way past `ManagedAICredentialsService.delete`'s provider-owned refusal. Keyword-only and defaulted off, so a new caller has to say the word.

**The 409 predicate is the row, not the report.** `ManagedAICredentialsService.delete(force=True)` reports blocked members *and deletes the parent anyway*, sweeping their stranded keys. Refusing on `result.blocked` therefore deleted the credential, answered `409`, and left a **lone provider row still carrying its `auto_provision_roles`** — the silent no-op this gate exists to prevent, produced by the gate. So the refusal fires only when the credential row is genuinely still there, which is where RESTRICT would otherwise trip, and it says why instead of raising an `IntegrityError` the admin cannot act on.

**The residual, stated rather than left to be discovered.** Revoking first means a forced delete that then fails to remove a member — a mint in flight, a lock timeout — leaves that member holding a key that no longer works at the vendor, and answers the second `409`. The admin retries and the retry completes the removal.

### The two conflict rules, at two different times

**Write time — `_validate_auto_provision_uniqueness`.** Moved here from `ManagedAICredentialsService` and now guards **provider vs provider** only: a managed credential has no auto-provision roles of its own to fight with. The transition scoping is preserved verbatim — only `claimed - previously_claimed` can raise. Validating the effective end state reads as safer and is not: it makes a stale absolute payload indistinguishable from a deliberate edit, so a client rebuilding its request from an open-time snapshot gets refused for someone else's change. `provider_id` is the row being written (excluded from the search), or `None` on create, where every slot is new. Raises `AIProviderConflictError`; the `409` mapping is the route's.

**Run time — `_apply_sdk_defaults` and `claim_held_slots`.** `claim_held_slots` is keyword-only with **no default** on `_apply_sdk_defaults` and `add_members`, because the two callers want opposite answers and a default would silently give one of them the other's.

| Caller | `claim_held_slots` | Why |
|---|---|---|
| `AccountProvisioningService`, and the minted materialise that follows it | `False` | Automatic provisioning never steals a default. Slot empty → claim it; slot held by anything → decline, and record a `ManagedDefaultSlotSkip` |
| `apply_to_existing`, `set_default_all` | `True` | Deliberate admin acts |
| `reconcile` | `True` | A superuser typing a member into `target_user_ids` has always claimed the slot for a **manual** record, and a manual record keeps every control it has today. Flipping this to incumbent-wins was tried and breaks `managed_ai_credential_auto_provision_test.py::test_claiming_a_slot_resets_the_override_but_holding_one_does_not_wipe_it`, which is that control asserted |

Two narrowings worth stating, because both look like bugs from the outside:

- **`reconcile` claims too.** The rule is not "provider grants never claim"; it is "*automatic provisioning* never claims". The table above is the whole of it.
- **The skip rides the grant result, not the reconcile result.** Given the row above, a field on the reconcile result would be empty on every response it ever appeared in — a worse lie than not offering it. `ManagedDefaultSlotSkip` is carried on `MemberAddition`, the add-only shape the automatic path returns, and from there up to `ProvisioningReport.default_slot_skips` → `InviteProvisioningSummary.default_slot_skips`.

For a **minted** provider the intent has to survive the request that expressed it, because the grant and the wiring happen on different requests. It is persisted on the membership row as `claim_held_default_slots` (migration `45938a69aee7`): without it, `claim_held_slots` never reached `materialise_minted_child` and the escape hatch silently stopped overwriting for exactly the provider kind per-user spend tracking is built on.

### `expiry_notification_date` can be set but not cleared

**A known limitation, with a stated fix path.** `AIProviderUpdate.expiry_notification_date` is `datetime | None = None`, where `None` means "leave this alone". So the date can be set, and moved, but there is no request that removes it. The Provider edit sheet states this in the field's helper text, which is invisible to whoever picks the problem up later — hence recording it here.

**It is inherited, not introduced.** `ManagedAICredentialUpdate.expiry_notification_date` has the identical signature (`datetime | None = None`) and the identical missing clear. Moving the field onto the provider carried the limitation across; this feature did not create it.

**A fix belongs on both surfaces or neither.** Fixing it only on the provider would leave the two update shapes disagreeing about whether the date can be removed, which is worse than a consistent limitation — an admin who learns the behaviour on one surface would be wrong about the other.

**The pattern to copy is the three-state `model_override_*` contract**, which solves exactly this shape one row up in the same model: omitted or `null` leaves the stored value alone, `""` clears it, and a value sets it. Applying that here means the empty string becoming a clear on both `AIProviderUpdate` and `ManagedAICredentialUpdate`, with the write-through the overrides already have.

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
| `admin.ai_provider.created` | Admin | `medium` | `POST /admin/ai-providers/` |
| `admin.ai_provider.updated` | Admin | `medium` | `PATCH /admin/ai-providers/{id}` |
| `admin.ai_provider.deleted` | Admin | `medium` | `DELETE /admin/ai-providers/{id}`. `details = {provider_id, forced, member_count, minted_key_count}` — **what the force overrode**, not merely that it was used. A forced delete of a `minted` provider destroys keys at the vendor, and this row is the last place anyone can learn how many |
| `admin.ai_provider.verified` | Admin | `medium` | `POST /admin/ai-providers/{id}/verify` |
| `admin.ai_provider.rotated` | Admin | `medium` | `POST /admin/ai-providers/{id}/rotate-key` — the fact of a rotation, never the before or after value |
| `admin.ai_provider.applied_to_existing` | Admin | `medium` | `POST /admin/ai-providers/{id}/apply-to-existing` — real run only; a dry run emits nothing, because there is nothing to audit about a question |
| `admin.ai_credential.auto_provision` | **New owner** | `low` | `AccountProvisioningService`, per grant at account creation. `details = {provider_id, managed_credential_id, target_user_id, origin, role, managed_by_id, actor: "system", provisioning_status}`, plus `child_credential_id` **when one exists** |
| `admin.ai_credential.auto_provision_failed` | **New owner** | `medium` | `AccountProvisioningService`, per provider that could not be granted. `details = {provider_id, managed_credential_id, target_user_id, origin, role, reason, actor: "system"}` |
| `admin.ai_credential.mint_requested` | Member | `medium` | A member added to a **minted** record (in place of `provision`, which names a credential), and each explicit Retry — the retry carries `retry_of: <last_error>` |
| `admin.ai_credential.minted` | Member | `medium` | `KeyProvisioningService`, on a successful mint. `details = {managed_credential_id, child_credential_id, target_user_id, external_key_ref}` |
| `admin.ai_credential.mint_failed` | Member | `medium` | Per failed attempt. `details = {managed_credential_id, target_user_id, reason, attempts, terminal}` — `terminal` distinguishes a backoff from a give-up |
| `admin.ai_credential.revoked` | `audit_user_id` | `medium` | A key destroyed at the provider |
| `admin.ai_credential.revoke_failed` | `audit_user_id` | **`high`** | A key we could **not** destroy. **This event is the durable record** — the membership row that carried the handles is gone by then (delete-then-revoke ordering), so without it the key is live at the provider with nothing naming it. The external ref goes in deliberately |
| `admin.ai_credential.revoke_blocked` | Member | **`high`** | A key deliberately left live because its child could not be deleted (`in_use_bundle`, `delete_failed`). Written from synchronous code by constructing the row directly, for the same reason `AccountProvisioningService` does |
| `external.account_config.read` | Calling user | `high` | `GET /external/account-config` (successful call only) |

`external.account_config.read` details: `{client_kind, external_client_id, provider_count, credential_ids}`.

The old `admin.ai_credential.provision_batch` event type from the previous per-row model is gone.

Automatic grants are **not** emitted by the admin route — they have no acting admin. They are siblings of `admin.ai_credential.provision` in the same namespace (different actor) so a reader of the security feed can tell an automatic grant from an admin's deliberate one without decoding the details blob. No key material is recorded in either.

**External key refs are recorded on purpose** in every minting and revocation event: they are what makes a leaked key nameable. Key material never is, anywhere.

`admin.ai_credential.auto_provision` omits `child_credential_id` entirely when the grant is a membership of a minted record, rather than stringifying a `None` into a field every other row of the feed reads as an id. The `admin.provider_admin_credential.*` family went with the route that emitted it; `admin.ai_provider.*` is where a provider act is recorded now. `set_default` events likewise skip members with no key.

**Whose feed a revoke lands in** is `RevocationRequest.audit_user_id`, not always the key holder — see [`RevocationRequest.audit_user_id`](#revocationrequestaudit_user_id).

---

## Model Catalog Integration

`ExternalAccountConfigService._resolve_model` imports from `backend/app/services/environments/model_catalog.py`:
- `resolve_model(engine, provider, mode, override, openai_compatible_model)` — returns the catalog default for a provider/engine combination
- `is_known_word(model_string)` — returns `True` for SDK-internal tier words (`"haiku"`, `"sonnet"`, `"opus"`) that cannot be used as Anthropic API model IDs
- `_strip_provider_prefix(model_string)` — strips the `provider/` prefix from discovered model IDs (e.g., `"anthropic/claude-3-5-sonnet-..."` → `"claude-3-5-sonnet-..."`)

---

## The platform-knowledge agent's API reference goes stale in a running environment

The platform-knowledge agent reads its own REST reference out of `/app/core/…/api_reference/`. Those files are generated from `frontend/openapi.json` by `.cinna-core-kit/scripts/sync_platform_knowledge.py` into `backend/app/env-templates/platform-knowledge-env/`, and `sync_platform_knowledge.py` also mirrors every non-`_tech` doc under `docs/application/` and `docs/agents/` into the same tree. **Edit the `docs/` originals; the mirror is regenerated, never hand-edited.**

**`/app/core` is a per-environment template copy, not baked into the image.** An environment created before this change keeps its own copy of that directory until it is rebuilt. So an agent running in an existing environment still reads `admin_provider_credentials.md` and `admin_provider_adapters.md`, still believes `/admin/provider-admin-credentials` and `/admin/provider-adapters` exist, and **will call them and get a 404** — and it will do so *after* the feature looks finished, because nothing about shipping the backend touches a running container's files. Rebuilding the environment is what updates it.

This is the same shape as the stale-template `404`s recorded elsewhere in this tree, and it is stated here rather than left as a support ticket.

---

## Dependencies

| Dependency | Used by |
|-----------|--------|
| `get_current_active_superuser` | Every `/admin/ai-providers/` route (`/adapters` included) and every `/admin/llm-providers/` route |
| `CurrentUser` | `/external/account-config`, `GET /ai-credentials/provisioning` |
| `CurrentClientClaims` | `/external/account-config` (reads `client_kind`, `external_client_id` from JWT) |
| `SessionDep` | All routes |

`CurrentClientClaims` is defined in `backend/app/api/deps.py` and returns `(client_kind, external_client_id)` from the JWT. It is shared with the rest of the external A2A surface.

---

## Tests

| File | Covers |
|------|--------|
| `backend/tests/api/ai_credentials/minted_ai_credentials_test.py` | 31 tests — minted-record create/update validation, membership statuses, converge, retry, revocation, the account lifecycle cascade, the owner-facing list and `api_key_onboarding_state` |
| `backend/tests/api/ai_credentials/admin_ai_providers_test.py` | The `/admin/ai-providers` routes. Replaces `provider_admin_credentials_test.py` and carries its security assertions across: the secret in no response body (asserted on the **raw key set**, so a key the response model never declared is visible), superuser-only on every endpoint including `/adapters`, the isolation invariant made executable against the generic credential surfaces, and the two structural router tests (every route declares a `response_model`; every declared model is in a reviewed set of secret-free projections, enumerated over `router.routes`). Also the delete gate as a route, the `minted` rotation `400`, the slot-conflict `409` envelope with its transition scoping, `extra="forbid"` on the create and on the nested config, and the adapters projection's before/after equality |
| `backend/tests/api/ai_credentials/ai_providers_service_test.py` | `AIProvidersService` at the service seam: the policy write-through and the cleared-override retraction, fixed-key rotation re-keying every member and minted rotation refused, the provider-vs-provider slot conflict and its transition scoping, `set_as_default` not being covered by it, incumbent-wins with its control and the escape hatch that does overwrite (including for a **minted** provider), the delete gate and its ordered removal, the provider-owned credential refusing a key / a key shape / a delete of its own, and the refusal of a manual credential created against a provider |
| `backend/tests/architecture/managed_credential_shadowed_fields_test.py` | No module outside `provisioning_policy.py` reads a shadowed field directly; also pins `SHADOWED_FIELDS` field-for-field |
| `backend/tests/architecture/ai_provider_isolation_test.py` | No query in `app/` selects `AIProvider` outside the two sanctioned modules. **No exception list** |
| `backend/tests/migrations/ai_provider_split_test.py` | Each abort shape of `c23d6b59a8f5`, the round trip over the five classifiable shapes, the downgrade refusal, and the RESTRICT direction |
| `backend/tests/unit/test_provisioning_policy.py` | The resolver branch, per field, against a credential whose own columns deliberately disagree with its provider's; plus the service's prospective claim matching the resolved one across seven configurations |
| `backend/tests/api/users/users_auto_provision_origins_test.py` | An invite and a signup of the same role grant the same set, inside the outbound-HTTP guard; a role change on an existing account provisions nothing |
| `backend/tests/api/users/users_invitation_lifecycle_test.py` | The `provider_ids` tri-state, `provider_not_found` in both its producers, a failure on the second provider keeping what the first granted, and a declined default slot disclosed rather than failed |
| `backend/tests/architecture/provider_adapter_registry_test.py` | 11 tests — the per-provider-table property and the adapter contract |
| `backend/tests/unit/ai_provider_adapters_test.py` | 31 tests — per-adapter behaviour |
| `backend/tests/utils/key_provisioning.py` | `converge_keys(db, *, limit=None)`, `make_membership_due(db, user_id)`, `mark_membership_minting(db, user_id)` |
| `backend/tests/utils/ai_provider.py` | `stub_all_providers`, `stub_minting_providers`, `probe_success` / `probe_skip` / `probe_invalid_key` |
| `backend/tests/stubs/key_provisioner_stub.py` | Subclasses `OpenAIKeyProvisioner` and overrides **`_call` alone**, so the spend-cap predicate, the null-secret rejection and the external-ref shape all still run for real. Installed through `registry.override_for_tests`, not `unittest.mock.patch`, and it **counts its invocations** so "the stub was never reached" fails loudly instead of passing quietly while the suite talks to the real provider |
| `backend/tests/stubs/provider_adapter_stub.py` | The recording adapter stub |

The `share_credential` refusal **is** now covered. It could not be reached through a route (`AICredentialShare` is unreachable from any route; the only caller is bundle install), so it is exercised **bundle-shaped** — through the install path that actually calls it — and **parametrized over both provisioning modes**, `minted` and `shared`, because the predicate is `is_admin_managed` and treating one mode as the interesting one is what produced the earlier gap. See `backend/tests/api/agents/bundles_install/agents_bundles_install_readiness_test.py` for the `publisher_credential_unshareable` gate verdict.

**Still not covered, knowingly:** the `adapter is None` branch of `detect_anthropic_credential_type` (`backend/app/utils.py`). It is marked `# pragma: no cover - the registry always serves it` and left **documented as unreachable** rather than covered by a test that would assert nothing — reaching it needs the registry to fail to serve a shipped provider, and a test that arranges that is testing the arrangement.

---

*Last updated: 2026-09-07 — **ai-credential-providers**. `provider_admin_credential` is renamed to **`ai_provider`** and extended with the rule (`auto_provision_roles`) and the whole wiring policy; `managed_ai_credential.provider_admin_credential_id` becomes `provider_id` with **ON DELETE RESTRICT** and loses `auto_provision_roles` and `provisioning_mode`. Migrations **`c23d6b59a8f5`** then **`45938a69aee7`**, in that order. The credential's same-named policy columns are **shadowed** rather than mirrored, and `provisioning_policy.resolve_policy` is the only legal read path, held by two architecture tests. `AIProvidersService` owns provider CRUD, `verify` (two questions by `kind`), `rotate_key` (`fixed_key` only), `apply_to_existing`, and an **`async` delete that awaits the revocations first**, because the thing being deleted is the secret they need. Two conflict rules at two times: provider-vs-provider `409` at write time, scoped to newly claimed slots, and **incumbent wins** at run time, where `claim_held_slots` is `False` for automatic provisioning and `True` for the deliberate acts (persisted on the membership row for minted providers). `/admin/ai-providers` replaces `/admin/provider-admin-credentials`; `GET /admin/ai-providers/adapters` replaces `/admin/provider-adapters`; `POST /admin/llm-providers/{id}/apply-to-existing` is removed; `/admin/llm-providers` creates manual records only and forbids unknown keys. A provider-owned credential cannot be deleted on its own (`400` naming the provider) and the forced provider delete no longer leaves that orphan behind its own gate. The invite path takes `provider_ids` and reports `provider_not_found`. `can_mint_now` is deleted from the adapters projection.*

*Previously — 2026-09-06 — zero-touch onboarding phase 5, **fifth pass** (review of the fourth pass's fixes): the `AICredentialShare` lookup moved **before** `is_shareable` in `InstallReadinessGate._scan_ai_credentials` and `InstallService._linkable_publisher_ai_credential` (an installer who already holds a share keeps the credential and is reported as missing nothing; existing share rows are left in place as an accepted trade); `PublishService.publisher_ai_credential_notices` → the required `AgentBundleRevisionPublic.publish_notices`, wired only by `POST /agents/{id}/publish` and rendered as a dismissible callout on the Bundle tab; `UserPublicWithAICredentials.api_key_onboarding_state` made **required with no default**, with the dashboard's `?? "needs_key"` removed, the wall latch re-based on `credentialsStatus !== undefined`, and the banner no longer suppressed by "Skip for now"; `_linkable_publisher_ai_credential` returns `None` for a vanished row (the FK made the old fallback claim an `IntegrityError`); `ai_credentials_service.has_a_default` deleted. See [Known Gap 14](admin_ai_credential_provisioning.md#known-gaps) for the escalated, deliberately unfixed unshareable-publisher-credential block.*

*Previously — **fourth pass** (whole-feature seam review): `is_shareable` widened from minted-only to every admin-managed child and raised as the typed `AICredentialNotShareableError`; `InstallService._linkable_publisher_ai_credential` made the documented install fallback real; the `publisher_credential_unshareable` gate reason, with `GateMissingReason` moved into `app/models/bundles/catalog.py`; `ManagedReconcileBlock.message` and its reason→sentence table; `AIKeyOnboardingState` made provider-agnostic via `owner_ids_with_a_default`, with the member-level projection and the latched dashboard wall; the mint claim token and its three race tests; `holds_provider_key` as the one key-holding predicate; the restored `ids_from_openai_shape` raise. Phase 5 itself: the `ai_providers` adapter registry, `provider_admin_credential`, `managed_ai_credential_membership` (membership as a row, with a backfill), `provisioning_mode` on the parent, `KeyProvisioningService` + its converge scheduler, the deactivation/reactivation/deletion revocation cascade, `GET /admin/provider-adapters/`, the member-retry route, `GET /ai-credentials/provisioning`, `api_key_onboarding_state`, and `app.core.db.leader_session`; migration `ed8d6a23f13c`. Phase 4 renamed the admin UI route to `/admin/ai-credentials` (backend prefix, tag, service and cache key unchanged). Phase 2: `auto_provision_roles` + per-mode model overrides (migration `b71863b32aa1`), `add_members` / `apply_to_existing`, `(role, mode)` slot-conflict validation, `AccountProvisioningService`*
