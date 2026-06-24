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
2. Owner searches for the recipient by name or email in the inline user picker (the shared `UserAllowlistPicker`, same pill UX as MCP / identity / bundle-grant sharing) and selects them to share immediately; existing recipients appear as removable pills. The picker is backed by `GET /users/search`, a lightweight authenticated endpoint (id / email / full_name only) available to non-admin owners — not the admin-only `GET /users/`. Users the credential is already shared with are filtered out of the results.
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

## `service_uri` Slot ID and the Per-User Token Pattern

### What `service_uri` is

`service_uri` is a plaintext, non-secret **audience/slot identifier** stored on a `Credential` row. The publisher stamps the same `service_uri` value onto both the connection credential that all installers share and onto each per-user token credential they pre-share with individual installers. Because the credentials have different human names, the existing name-based matcher cannot link them automatically — `service_uri` solves this by giving the matcher a stable, name-independent slot to key on.

Format is an opaque publisher-chosen string. Convention is a URI-like discriminator, for example `agent-b://company-scope-token`. The value is never encrypted, never carries authority, and does not gate access — authority lives in the token value itself, validated server-side inside the producer agent.

### Install-time Auto-Prefill Matcher — Full Precedence Order

`CredentialsService.find_match_for_spec` tries tiers in order and returns on the first hit:

| Tier | Name | What it matches | Notes |
|------|------|-----------------|-------|
| **0a** | `service_uri` — owned | Installer owns a credential where `type == spec.type AND service_uri == spec.service_uri` | Newest by `id desc` when multiple match |
| **0b** | `service_uri` — shared | A `CredentialShare` grants the installer access to a credential where `type == spec.type AND service_uri == spec.service_uri` | Newest by `id desc` when multiple match |
| **1** | Owned name + type | Case-insensitive `(lower(name), type)` match on owned credentials | |
| **2** | Shared name + type | Same match through `CredentialShare` | |
| **3** | Type-only fallback (PBU only) | Exactly one owned credential of the right type, any name | Not attempted for PBT specs |
| **PBT value anchor** | (PBT path) | Non-private field values equal `template_data` exactly | Runs after name tiers, only for PBT specs |

Tiers 0a and 0b short-circuit the entire chain — they win even over the PBT value-anchor check (OQ1 resolution). When `service_uri` is `NULL` or not set on the spec, Tiers 0a/0b are skipped entirely and the function is equivalent to pre-feature behavior (full backward compatibility, I5).

When two credentials collide at Tier 0a (an installer owns two credentials with the same `service_uri` and type), the most recently created one wins. The `service_uri` value should be unique per (user, slot) to avoid this.

### Two-Credential Bundle Pattern

A bundle that exposes a producer agent's per-user-scoped API ships **two** credentials:

1. **Connection credential** (`agent_api` type, `provided_by="publisher"` / PBP) — the narrowed proxy URL and a single shared token that all installers use. The publisher enables `allow_sharing=True`, and the bundle delivers it to every installer via the existing PBP flow. All installer traffic routes through the publisher's single producer environment. This credential carries **no** per-user authority.

2. **Per-user second token** (`api_token` type, `provided_by="user"` / PBU) — carries the specific company or scope the publisher has granted to that installer. The publisher pre-creates one per installer, stamps the same `service_uri` as the connection credential, and shares it to the correct user before the user installs. At install time the Tier 0b matcher finds the shared token and auto-suggests it — the experience is "install and it just works." Authority is validated server-side inside the producer agent; the platform proxy enforces `policy.yaml` method/rate constraints at the edge.

Revoking a user's access = revoking the `CredentialShare` for that user's second token. The connection credential's share is independent and is not affected. When a user has no second token (none pre-shared, or the share was revoked), the runtime gate leaves the install in `needs_setup` — no token, no access.

### Share-Before-Install Ordering Constraint

Auto-prefill runs **at install time** — the matcher queries the installer's accessible credentials at the moment `POST /catalog/{bundle_id}/install` is called. This means per-user credentials must be shared with the installer **before** they install the bundle. If the token is shared after the install, the install has already run the matcher and created a `needs_setup` placeholder for that spec. The user must then link the now-shared token manually from the agent's **Credentials tab** — there is no automatic re-match in the current implementation.

Recommended publisher workflow:
1. Pre-create each per-user second token with the correct `service_uri`.
2. Share each token to the intended installer's email via the Credential Sharing UI.
3. Only after sharing, notify the user to install the bundle (or publish/grant catalog access).

If a token is shared late, the fallback is: user opens the Credentials tab on the installed agent, finds the placeholder row marked "Setup needed", and uses the credential picker to switch it to the pre-shared token.

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

### `provided_by` is frozen at publish — republish to change it

`provided_by` is snapshotted into the revision's `required_credential_specs` at publish time and is **immutable** for that revision. Installers read the snapshot verbatim. The Bundle tab's credential-provisioning panel, by contrast, recomputes `provided_by` **live** from the credential's current `allow_sharing` / override — so the instant the publisher toggles sharing, the panel shows the new mode even though no new revision exists yet.

This is the expected source of a publish-vs-live divergence: a publisher who enables sharing (or changes the override) **after** the last publish sees the panel say "Embedded (shared)" while installers still receive the previously published `"user"` spec. The platform surfaces this gap rather than silently mutating the revision: `GET /agents/{agent_id}/bundle-credential-drift` (publisher-install owner-only, 404 leak-safe) returns a per-credential `live_provided_by` vs `snapshot_provided_by` diff, and the provisioning panel renders an amber **"republish to apply"** hint on each drifted row. Republishing writes a fresh snapshot and clears the drift. Because the drift computation reuses the same `resolve_provided_by` (live) and `parse_credential_spec` (snapshot) as publish/install, the hint can never disagree with what installers actually receive.

`agent_api` connection credentials are the common trigger for this, because the "Connect Agent API" helper always creates them with `allow_sharing=False` and the publisher enables sharing afterwards. They can be `"publisher"` (PBP) or `"user"`, but never `"template"` — a connection has no user-fillable private fields, and the provisioning panel omits the Template option for `agent_api`.

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

**Bundle membership disclosure (all tiers / all modes).** In addition to the Tier-2 PBP block, `CredentialDeletionImpact` carries a `bundle_usages` field that lists every bundle whose publisher install links the credential, regardless of provisioning mode (`publisher`, `template`, or `user`). This field is purely informational — it does not affect the tier classification or block logic. The delete dialog always shows a "Used in bundles" section when `bundle_usages` is non-empty, so a credential that is template-provided (PBT) or user-provided (PBU) in a bundle, or publisher-provided with zero active installs, is now disclosed to the owner even when the deletion would otherwise proceed without a block. The `bundle_pbp_usages` field remains the subset that exclusively drives the Tier-2 block and install-count accounting.

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

## Credentials Page: Filter Tabs and Category Assignment

The `/credentials` page organises credentials into three **filter tabs** (mirroring the Catalog page's filter-pill idiom): **My Credentials** (default), **Automatic Credentials**, and **Bundle Credentials**. The old stacked "My Credentials / Automatic Credentials / Shared with Me" sections are replaced by this single tab bar.

Tab membership is driven by a server-computed `category` field (`"mine"` | `"automatic"` | `"bundle"`) returned on every `CredentialPublic` and `SharedCredentialPublic` response. The frontend filters the merged owned+shared list purely on this field — no client-side re-derivation.

### Categorization rules

The single source of truth is `CredentialsService.classify_credential_category(*, is_owned, credential_type, share_source)`, which holds `AUTOMATIC_TYPES = {CredentialType.AGENT_API, CredentialType.MCP_PROVIDER}`:

| Credential | Rule | Tab |
|-----------|------|-----|
| Owned, type ∈ `{agent_api, mcp_provider}` | Automatic types → automatic | **Automatic Credentials** |
| Owned, any other type | | **My Credentials** |
| Shared (received), `share.source == "bundle_install"` | Bundle-installed → bundle | **Bundle Credentials** |
| Shared (received), `share.source ∈ {"direct", NULL}` | NULL is read as "direct" | **My Credentials** |

`agent_api` and `mcp_provider` connection credentials are always owned (a shared `agent_api` credential still belongs to the recipient as an owned credential after connect); they appear under Automatic Credentials regardless of whether they are shared further.

### URL hash navigation

The active tab is reflected in the URL hash: `#my`, `#automatic`, or `#bundle`. Absent or unknown hash defaults to My Credentials. Changing tabs updates the hash; back/forward navigation restores the tab.

## `CredentialShare.source` Provenance Marker

A nullable `source` column (`varchar(20)`) on the `credential_shares` table records **how** a share was created:

| Value | Meaning |
|-------|---------|
| `"direct"` | Created by the owner explicitly sharing with a specific user |
| `"bundle_install"` | Created automatically when an installer installed a bundle that provides this credential (PBP flow) |
| `NULL` | Legacy row (pre-feature); read as `"direct"` everywhere |

The value is **stamped at creation time, never updated after the fact**. Two code paths stamp it:

- `CredentialShareService.share_credential(...)` — the direct-sharing path — stamps `source="direct"`.
- `InstallService._try_link_publisher_credential(...)` — the PBP install path — stamps `source="bundle_install"` on **insert only** (see first-writer-wins below).

**First-writer-wins re-install rule:** if a `CredentialShare` row already exists (because the owner previously shared the credential directly with this user, OR because the bundle was already installed), the insert is skipped and the existing `source` is never overwritten. This means:
- A pre-existing `source="direct"` share survives a later bundle install unchanged → the credential stays in **My Credentials** for that recipient.
- A pre-existing `source="bundle_install"` row is unaffected by a subsequent direct share (the unique constraint prevents a second row; the owner would need to revoke the bundle share first).

**Publisher workflow implication:** the publisher maintains a **single who-can-install list** (the `CredentialShare` table). Installing the bundle auto-adds the installer to that list with `source="bundle_install"`. There is no separate "directly shared" list to maintain — the provenance marker is the only difference between the two kinds of share.

## Architecture Overview

```
Direct / Full Sharing:
Owner enables sharing → Shares credential by recipient email
         │
         └→ CredentialShare record created (source="direct")
                    │
                    ├→ Recipient sees credential in "My Credentials" tab
                    ├→ Recipient links shared credential to their agents
                    └→ Agent environments receive shared credential data (same as owned)

Owner revokes share → CredentialShare record deleted → Immediate access removal

Bundle Install (PBP):
Installer installs bundle with publisher-provided credential
         │
         └→ _try_link_publisher_credential (insert path only)
                    │
                    └→ CredentialShare created (source="bundle_install")  [idempotent]
                                │
                                └→ Recipient sees credential in "Bundle Credentials" tab

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
