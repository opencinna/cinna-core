# Credential Sharing

## Purpose

Enables users to share their credentials with other users, allowing recipients to use shared credentials in their agents without exposing the actual credential values (passwords, tokens, etc.). In the bundle/install system, credential sharing also covers two publisher-facing modes: full sharing (publisher's live credential passed to every installer) and template sharing (publisher ships non-private fields as defaults; the installer fills in the private ones).

## Core Concepts

- **Credential Share** - Association record granting a recipient read-only access to another user's credential
- **Shareable Credential** - A credential with `allow_sharing=true`, enabling the owner to share it directly
- **Template Credential** - A credential with `allow_template_sharing=true`; its non-private fields are copied as defaults for new installers, who must supply the private fields themselves
- **Share Recipient** - User who received a credential share; can link it to their own agents but cannot view credential values
- **Access Level** - Currently only `read` access; recipients can use but not modify or view sensitive data

## User Stories / Flows

### Sharing a Credential (direct / full sharing)

1. Credential owner enables sharing on a credential (`allow_sharing=true`)
2. Owner shares credential with another user by entering their email
3. Recipient sees credential in "Shared with Me" section of the Credentials page
4. Recipient can link shared credential to their agents via the Agent Credentials tab
5. Shared credential functions identically to owned credentials in agent environments

### Revoking Access

1. Owner revokes a specific share from the credential's sharing management panel
2. Alternatively, owner disables sharing entirely (`allow_sharing=false`)
3. Disabling sharing immediately revokes ALL existing shares (destructive, with confirmation). When the credential is publisher-provided (PBP) in published bundles, the disable-sharing dialog also surfaces the blast-radius data (same `GET /credentials/{id}/deletion-impact` cache as the delete dialog) so the publisher can see which bundles and installs will break before confirming.
4. Deleting a credential is **blast-radius-gated** (see Deletion Impact Gate below).

### Template Sharing (bundle context)

Template sharing is designed for bundle publishers who want to pre-configure part of a credential for every installer — for example, an Odoo credential where the server URL and database name are fixed for all users of the bundle, but each installer has their own `login` and `api_token`.

**Publisher setup:**

1. Publisher enables template sharing on a credential (`allow_template_sharing=true`)
2. Publisher marks specific `credential_data` field names as private via `template_private_fields` (e.g. `["login", "api_token"]` for an Odoo credential). Fields not in this list — such as `url` and `database_name` — are treated as the shared template payload
3. Publisher assigns this credential as `provided_by="template"` for the relevant bundle spec, either through the Credential provisioning panel on the Bundle tab or via inference (when `allow_sharing=false` and `allow_template_sharing=true`, inference yields `"template"`)
4. At publish time, `PublishService._template_payload_for` decrypts the credential, strips the private fields, and stores only the non-private values in `spec["template_data"]` within `required_credential_specs`. The `spec["template_private_fields"]` list is also stored so the install screen and runtime gate know which fields are missing

**Installer experience:**

1. The install screen labels the spec's accordion item with the field names the installer must supply (from `template_private_fields`)
2. On install, `InstallService._materialise_template_credential` creates a fresh `Credential` row owned by the installer:
   - `encrypted_data` is seeded from `template_data` (non-private values, pre-filled)
   - `is_placeholder=True` — the runtime gate keeps the install in `needs_setup` until private fields are supplied
   - `allow_sharing=False` and `allow_template_sharing=False` — the installer's copy is private; downstream re-sharing requires an explicit toggle
   - `template_private_fields` mirrored from the spec so the setup page can highlight empty fields
3. The installer is directed to `/agent/{id}/setup-credentials` — the setup page surfaces the credential via `GET /agents/{agent_id}/setup-credentials`, which returns `SetupCredentialSummary` items including:
   - `template_private_fields` — the list of fields to fill
   - `template_prefilled_data` — the non-private values already present, shown as read-only context
4. When the installer fills in the private fields via `PUT /agents/{agent_id}/setup-credentials/{credential_id}`, `CredentialsService.update_credential` runs a completeness check: if all required fields for the credential type now pass, `is_placeholder` is flipped to `False`. The runtime gate then re-runs; if the install is ready, `INSTALL_SETUP_COMPLETED` fires over WebSocket and the setup banner clears

**If the installer already owns a suitable credential:**

The installer can choose `mode="use_existing"` on the install form. `_setup_install_credentials` detects this and skips template materialisation — linking the existing credential directly.

## Business Rules

### Sharing Modes Summary

| Mode | Flag(s) | `provided_by` | Installer receives |
|------|---------|---------------|-------------------|
| User provides | `allow_sharing=false`, `allow_template_sharing=false` | `"user"` | Empty placeholder to fill in |
| Full sharing | `allow_sharing=true` | `"publisher"` | Publisher's live credential via `CredentialShare` |
| Template sharing | `allow_template_sharing=true` | `"template"` | Fresh credential pre-filled with non-private fields; installer fills in private fields |

The two flags can both be `true` simultaneously. `provided_by` resolution at publish time:

1. Publisher's per-spec override in `Agent.publish_settings.credential_overrides[<name>].provided_by` if present (`"user"`, `"publisher"`, or `"template"`)
2. Inference fallback: `allow_sharing=True` → `"publisher"`; else `allow_template_sharing=True` → `"template"`; else `"user"`

Validation (enforced by `PublishService._validate_publisher_provides` before snapshot is written):
- A spec resolved as `"publisher"` requires `allow_sharing=True` on the underlying `Credential` row
- A spec resolved as `"template"` requires `allow_template_sharing=True` on the underlying `Credential` row

### Direct Sharing States

| State | Description | Transitions |
|-------|-------------|-------------|
| `allow_sharing=false` | Credential cannot be directly shared | Enable sharing |
| `allow_sharing=true` | Credential can be shared with users | Share with user, Disable sharing |
| Shared | CredentialShare record exists for a recipient | Revoke share |

### Deletion Impact Gate

Deleting a service credential now goes through a graduated blast-radius check. Before performing the delete the service calls `get_deletion_impact`, which classifies the operation into one of three tiers:

| Tier | Condition | Outcome |
|------|-----------|---------|
| **0** (self-only) | Credential linked only to owner's own agents; no `CredentialShare` rows; not PBP in a published bundle with foreign installs | Delete proceeds. UI lists affected own agents. |
| **1** (direct shares) | At least one `CredentialShare` exists, but the credential is not PBP in a published bundle with active foreign installs | Delete proceeds with a warning: "N users will lose access immediately." |
| **2** (PBP in published bundle, active installs) | Credential is publisher-provided in a published bundle AND has at least one active foreign install | Non-forced `DELETE` returns **HTTP 409** with the structured `CredentialDeletionImpact` payload. The owner can pass `?force=true` to override. The UI shows the affected bundles, the install count, and a "Force delete & break installs" button. On force-delete the affected installs degrade to `publisher_broken` state at runtime (the `InstallReadinessGate` detects the missing PBP credential). |

Important scoping: `active_install_count` in the impact payload is restricted to installs of the PBP bundle(s). Direct-share recipients who have linked the same `Credential` row to their own agents are counted in `direct_share_count` (Tier 1), not `active_install_count`, so the two tiers cannot over-count each other.

PBT (template) installs materialise an independent copy of the credential owned by the installer — they are unaffected by deletion of the publisher's original row and do not count toward Tier 2.

AI credentials (LLM provider keys) follow the same 409 / force pattern but have only **Tier 0** and **Tier 2**. Tier 2 for AI credentials means the credential is referenced by a published bundle as a publisher-provided AI credential (`publisher_ai_credential_conversation_id` / `publisher_ai_credential_building_id`). There is no Tier 1 (no direct AI credential shares). A forced delete nulls the FK via `ON DELETE SET NULL`, degrading the bundle back to "user provides". The force button reads "Force delete & degrade bundles".

### Constraints

- Cannot share credentials with `allow_sharing=false`
- Cannot share with yourself
- Cannot create duplicate shares (same credential + same user)
- Cannot share with non-existent users
- Disabling `allow_sharing` immediately revokes ALL existing shares
- Deleting a credential cascades to delete all shares (after passing the blast-radius gate described above)
- Private fields (from `template_private_fields`) are never stored in the bundle revision JSON; `PublishService._template_payload_for` strips them before writing `template_data` to the manifest

### Access Model

- **Owner** - Full control: view/edit/delete credential, manage shares, see credential values
- **Recipient (full share)** - Read-only: view metadata, link to own agents, use in environments; cannot see values, edit, or delete
- **Recipient (template share)** - Owns their own credential row pre-seeded with non-private values; can fill in their private fields and modify their copy; publisher's actual private values are never exposed
- Credential values (`encrypted_data`) are never exposed to share recipients in either mode
- Revoking a direct share immediately removes the recipient's access; template materialised rows are independent copies and are unaffected by the publisher revoking template sharing after install

### Role Gating

- The Sharing card on the credential detail page (sharing toggle, share dialog, shares list) is hidden for `agent-user` accounts — they don't share their credentials with anyone. The card is rendered only for `agent-developer` and `admin` owners. See [User Roles](../../application/user_roles/user_roles.md) for the role model.

## Architecture Overview

```
Direct / Full Sharing:
Owner enables sharing → Shares credential by recipient email
         │
         └→ CredentialShare record created
                    │
                    ├→ Recipient sees in "Shared with Me" UI section
                    ├→ Recipient links shared credential to their agents
                    └→ Agent environments receive shared credential data (same as owned)

Owner revokes share → CredentialShare record deleted → Immediate access removal

Template Sharing (bundle context):
Publisher sets allow_template_sharing=true + marks private fields
         │
         └→ Publish → template_data (non-private) written to revision spec
                    │
                    └→ Install → _materialise_template_credential
                                  │
                                  ├→ Fresh Credential row (installer-owned, is_placeholder=True)
                                  ├→ encrypted_data seeded from template_data
                                  └→ Installer fills private fields on setup page
                                            │
                                            └→ is_placeholder flips False when complete
                                               → INSTALL_SETUP_COMPLETED event
```

## Integration Points

- [Agent Credentials](agent_credentials.md) - Shared and template-materialised credentials link to agents and sync to environments identically to owned credentials
- [Agent Bundles & Installs](../agent_bundles/agent_bundles.md) - `required_credential_specs` in a bundle revision drives install-time credential handling for all three modes (`provided_by`: `"user"`, `"publisher"`, `"template"`); template specs carry `template_data` and `template_private_fields`
- [User Workspaces](../../application/user_workspaces/user_workspaces.md) - Credentials exist within workspace context
