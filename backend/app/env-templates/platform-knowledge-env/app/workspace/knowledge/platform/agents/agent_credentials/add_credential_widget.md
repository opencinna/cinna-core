# Add Credential Widget

## Widget Purpose

Modal launched from the Credentials page "Add Credential" button. Provides a one-click type picker: the user sees all credential types as coloured badges grouped by category, picks one, and is taken straight to the credential detail page with a default name pre-filled and the Name field focused for immediate rename.

Replaces an earlier two-step wizard (enter name → pick type → submit → form) that required the user to commit to a name before knowing what fields the type needed.

## User Flow

1. User clicks **Add Credential** on the Credentials page.
2. Modal opens with a search box at the top (autofocused) and four category groups:
   - **API & Access** — `api_token`, `ssh_key` (slate palette)
   - **Email** — `email_imap`, `email_smtp` (amber palette)
   - **Google** — `gmail_oauth`, `gmail_oauth_readonly`, `gdrive_oauth`, `gdrive_oauth_readonly`, `gcalendar_oauth`, `gcalendar_oauth_readonly`, `google_service_account` (blue palette)
   - **Applications** — `odoo` (violet palette)
3. User optionally filters the list by typing in the search box — match runs against label, group name, and a per-type keyword blob (e.g. "inbox" matches IMAP, "deploy" matches SSH key).
4. User clicks a badge. The widget immediately fires a `POST /api/v1/credentials/` with a sensible default name (e.g. "Gmail (Read-Only)", "SSH Key"). A spinner appears on the clicked badge; all other badges are disabled while the request is in flight.
5. On success:
   - Modal closes.
   - Navigation goes to `/credential/{id}?new=1`.
   - Detail page latches the `new=1` marker, strips it from the URL via `replace`, and calls `form.setFocus("name", { shouldSelect: true })` so the user can rename in-place.
6. On error: the spinner clears, the modal stays open, an error toast is shown, and the user can retry.

## Special Cases

- **ssh_key** — the backend requires `credential_data` at creation, so the widget sends `{ mode: "generate", key_type: "ed25519" }` by default. A fresh key pair is generated server-side; the user sees the resulting public key + fingerprint on the detail page (which uses `SSHKeyEditView`). The name field is focused there via the same `focusNameField` mechanism (no separate wizard step for SSH keys any more).
- **All other types** — created with only `{ name, type, user_workspace_id? }`; the user fills in `credential_data` on the detail page.
- **Wrong type chosen** — because creation happens on click, an unwanted type selection leaves a placeholder credential the user must delete manually. Accepted trade-off for removing the name-first step.

## Component Structure

- `frontend/src/components/Credentials/AddCredential.tsx` — owns the trigger button, the Dialog, the search state, the grouped badge grid, and the create mutation. Imports `CREDENTIAL_TYPE_GROUPS` from the shared registry below.
- `frontend/src/components/Credentials/credentialTypes.ts` — shared credential-type metadata registry. Exports `CREDENTIAL_TYPE_GROUPS` (per-group `badgeClass` + per-type `label` / `defaultName` / `keywords` / `icon`) and the `getCredentialTypeMeta(type)` helper that flattens it into a single per-type lookup (with a neutral slate fallback for unregistered types). Single source of truth — keeps the picker and any display badges visually in sync.
- `frontend/src/components/Credentials/CredentialTypeBadge.tsx` — display-only `<span>` chip that renders the icon + label + palette for a credential type by reading from `getCredentialTypeMeta`. Same pill shape, icon size, and Tailwind palette as the picker chips, but non-interactive. Reused on the publisher credential-provisioning panel (`Agents/CredentialProvisioningSection.tsx`).
- `frontend/src/routes/_layout/credential/$credentialId.tsx` — consumes the `?new=1` search param via `validateSearch`, passes a latched `focusNameField` boolean into `OwnedCredentialView` and `SSHKeyEditView`.
- `frontend/src/components/Credentials/CredentialForms/SSHKeyEditView.tsx` — accepts the `focusNameField` prop and triggers `form.setFocus("name", { shouldSelect: true })` on mount.

Icons come from `lucide-react`: `Key`, `KeyRound`, `Inbox`, `Send`, `Mail`, `HardDrive`, `Calendar`, `ShieldCheck`, `Briefcase`.

## State Management

All local to `AddCredential`:

- `isOpen: boolean` — Dialog open state.
- `query: string` — search input, drives client-side filtering via a `useMemo` over `CREDENTIAL_TYPE_GROUPS`.
- `pendingType: CredentialTypeKey | null` — which badge is currently creating (drives the per-badge spinner).
- `createMutation` — `useMutation` wrapping `CredentialsService.createCredential`. Invalidates the `["credentials"]` query on settle.

On the detail page side, `focusNameField` is captured once with `useState(() => search.new === 1)` so a refresh doesn't re-focus the input.

## API Interactions

- `POST /api/v1/credentials/` — creates the credential. See `backend/app/api/routes/credentials.py:create_credential()`.
- `GET /api/v1/credentials/{id}` / `GET /api/v1/credentials/{id}/with-data` — fetched by the detail page after navigation; unchanged by this widget.

No new backend endpoints or service methods were introduced by the widget — the server-side SSH-key default path (`mode=generate, key_type=ed25519`) already existed via `CredentialsService.process_ssh_key_credential_input()`.

## Integration Points

- [Agent Credentials](agent_credentials.md) — lists the credential types and their fields; the widget's group layout mirrors those types.
- [SSH Key Credentials](ssh_key_credentials.md) — generation flow invoked for the default `ssh_key` creation path.
- [Credential Sharing](credential_sharing.md) — sharing UI lives on the detail page the widget navigates to.
