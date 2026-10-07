---
feature: desktop_credentials
domain: application
one_liner: "Lets a signed-in Cinna Desktop session copy an owner's locally-compatible cloud credentials onto a local agent, gated by owner consent and a fixed type allowlist."
docs:
  tech: desktop_credentials_tech.md
affects: [local_agent_kit]
---

# Desktop Credential Delivery

## Purpose

Cinna Desktop can list and copy a user's own cloud credentials onto a locally-run agent, so a
skill built with the [Local Agent Kit](../local_agent_kit/local_agent_kit.md) can use the same
Odoo login, API token, or mailbox the user already configured on the platform, without retyping
secrets into a local `.env` file. Only credential types that are self-contained and usable on a
plain computer are ever eligible, and only with the credential owner's explicit, separate consent.

## Core Concepts

- **"Mine" category** — the only credentials Desktop may ever see or copy are the caller's **My
  Credentials** (owned credentials other than automatic ones, and credentials shared directly with
  them). Automatic credentials (Agent API connections, agent2agent MCP providers) and bundle/skill
  install credentials are never listed or delivered, regardless of type or consent. See
  [Agent Credentials — Credentials Page: Filter Tabs](../../agents/agent_credentials/credential_sharing.md#credentials-page-filter-tabs-and-category-assignment)
  for how a credential earns the "mine" category.
- **Local-use consent (`allow_local_use`)** — a per-credential switch, off by default, that only the
  *owner* of a credential can set. It is independent of `allow_sharing` (which controls whether the
  credential can be shared at all): a recipient needs both a live share and the owner's consent
  before Desktop will copy the value onto their machine. The owner's own copy never needs consent.
- **Locally compatible type** — one of a fixed, small allowlist of credential types that are
  self-contained enough to work on a plain computer: `email_imap`, `email_smtp`, `odoo`,
  `api_token`, `google_service_account` (its full service-account key JSON is delivered alongside
  the entry). Every other type is refused, listed nowhere, and delivered to no one — see Business
  Rules.
- **Refusal reason** — materialization is per-item, never batch-fatal: each requested credential
  either comes back with a value or a typed reason (`not_found`, `no_access`,
  `not_local_category`, `local_use_not_allowed`, `unsupported_type`, plus the issued-key reasons
  `key_revoked`, `key_expired`, `key_paused`). A Local Agent Kit script sees the same reason as
  `unavailable_reason` on the entry.
- **Issued API key (`agent_api`, opt-in)** — a key a producer's owner issued *to the caller* (see
  [Agent REST API — Issued Keys](../../agents/agent_api/agent_api.md#issued-keys)). It is the one
  `agent_api` shape Desktop can carry, because identity is bound to the key and a local script can
  call the producer with a plain bearer token. A Desktop client must ask for keys explicitly
  (`include=agent_api_keys`) on both the list and the materialize call; without that flag no
  `agent_api` item is listed and any `agent_api` id is refused `unsupported_type`, so a Desktop
  version that predates keys can never receive one it would mislabel.

## User Stories / Flows

### Owner allows Desktop use on their own credential

1. Owner opens a credential's detail page and enables sharing if they intend to share it, or
   leaves it private for their own Desktop use.
2. If the credential's type is locally compatible, the sharing card shows an **"Allow use on
   recipients' computers"** switch (hidden entirely for incompatible types). Turning it on sets
   `allow_local_use=true`. Turning it back off does **not** recall values a Desktop session already
   copied — the UI says so next to the switch.
3. The owner's own Cinna Desktop session can always copy the owner's own "mine" credentials of a
   compatible type — the owner needs no consent switch for their own use.

### Desktop lists and attaches a credential to a local agent

1. Desktop calls the metadata list endpoint with its stored ETag; an unchanged list answers 304
   with no work done server-side.
2. Desktop shows the user their "mine" credentials of a compatible type, each flagged
   `local_use_allowed` (true for owned rows and for shared rows the owner has consented to, false
   otherwise so Desktop can explain why a listed row is not yet usable).
3. The user picks which credentials to attach to a specific local agent; Desktop requests those ids
   for materialization.
4. For each accepted id, Desktop receives the same shaped entry the cloud writes into a running
   environment's `credentials.json` (plus, for a service account, the standalone key file), writes
   it into the agent's local `credentials/credentials.json`, and the agent's
   `scripts/cinna_credentials.py` reader picks it up the same way a cloud agent would. See
   [Local Agent Kit](../local_agent_kit/local_agent_kit.md).
5. For each refused id, Desktop learns why (not found, no access, wrong category, consent not
   given, or an incompatible type) and can explain the gap to the user instead of silently omitting
   the credential.

### Recipient copies a credential shared with them

1. A credential was shared directly with the recipient (`allow_sharing=true` on the owner's side,
   and the recipient appears in the owner's share list) — see
   [Credential Sharing](../../agents/agent_credentials/credential_sharing.md).
2. The owner has separately turned on **local-use consent** for that credential.
3. The recipient's Desktop lists it (`local_use_allowed=true`) and materializes it like any other
   "mine" credential.
4. The owner is notified: a security event fires on the owner's own account naming the recipient,
   distinct from the ordinary delivery event fired on the requester's account. See
   [Credential Security Hardening](../../agents/agent_credentials/credential_security_hardening.md).

## Business Rules

- **Access order is fixed and fails closed early.** A requested credential is refused in this
  order: does it exist; does the caller have access (owner, or a live share with `allow_sharing`
  still true); is it in the "mine" category; did the owner consent (`allow_local_use`, skipped for
  the owner's own credentials); is its type locally compatible. A caller without access learns
  nothing else about the credential — the response cannot distinguish "wrong category" from "no
  access" for someone who was never allowed to see it in the first place, because the access check
  runs first.
- **Superuser status is not a bypass.** Every rule above applies the same way to a superuser as to
  anyone else; there is no admin override for exporting another user's credential values.
- **The compatible-type allowlist is small and deliberate.** The six Google OAuth types (Gmail,
  Drive, Calendar and their read-only variants) are excluded because refreshing their access token
  needs the OAuth client secret, which only the backend server holds — a copied OAuth entry would
  simply stop working once its access token expired, with no way for the local machine to renew
  it. SSH keys are excluded because the private key material never leaves Core in the first place.
  Agent API **connection** credentials and MCP provider credentials are excluded because they are
  not portable credential files: a connection is bound to the platform's anonymous agent-to-agent
  model, and an MCP provider credential is an SDK MCP-server manifest entry, not
  something a local script can read a value out of. **Desktop credential delivery never refreshes
  an OAuth token as part of materialization** — the question does not arise, because OAuth types
  are never listed or delivered at all.
- **Issued keys: eligibility and delivery.** An `agent_api` credential is eligible only when the
  client opted in, the caller **owns** it, it is an issued key (never a connection) and it was
  **issued to the caller**; every other `agent_api` credential is `unsupported_type` and never
  listed. Eligible keys are listed with their status; only an **active** key is delivered. Others
  are refused per item with `key_revoked` (revoked by the issuer, producer removed, issuer
  removed: terminal), `key_expired` (terminal) or `key_paused` (API disabled or the producer's kill
  switch off: recovers by itself, so Desktop keeps polling), each carrying the detailed
  `key_status`. A key is exempt from the `allow_local_use` consent (it is the caller's own).
- **Public origin rule.** A delivered key's `base_url` / `spec_url` are the **public** origin
  (`BACKEND_BASE_URL`, else `WEBHOOK_BASE_URL`, else `FRONTEND_HOST`), recomputed on every
  list/materialize, never the Docker-internal origin agents use in the cloud. Operators must
  configure Desktop's server URL to that origin; Desktop logs a mismatch, Core does not guess.
- **Revision.** A key's `revision` is a hash of its `updated_at`, its read-time status, its expiry
  and its public `base_url`, so rotating, revoking, pausing, a producer/issuer removal, expiry and a
  change of public origin all change the revision (and the list ETag) with no write. Other
  credentials keep their revision rule.
- **Rotation reaches a running local agent.** The previous value keeps working for 5 minutes after a
  rotate, and the kit helper re-reads the credential files on every request.
- **The session must be interactive.** Materialization requires a live, non-revoked Desktop client,
  and specifically one whose current token was **not** obtained through the CLI account-token
  exchange (see [Desktop App Authentication — CLI account-token
  exchange](../desktop_auth/desktop_auth.md#cli-account-token-exchange)) — a session minted that
  way is refused secret export even though it is otherwise a normal, live desktop session.
- **Rate limited.** At most 60 materialize calls per minute per (user, Desktop client) pair; over
  the limit the request is refused with a retry hint. This is a per-process limiter, not a
  distributed quota.
- **Every delivery is audited, values are not.** Each delivered credential logs a security event on
  the requester's account naming the credential and the Desktop client. When the delivered
  credential was shared with the requester by someone else, a second event logs on the *owner's*
  account naming the recipient, so an owner can see who has copied their secret locally. No event
  ever carries the credential's value.
- **Disabling local-use consent, or revoking a share or an issued key, does not recall a value already copied.** Both
  the sharing UI and this doc are explicit about that: consent gates future materialization, it is
  not a remote wipe.

## Architecture Overview

```
Cinna Desktop (interactive session)
    │
    ├─ GET /external/credentials  (metadata, ETag/304)
    │       └─ DesktopCredentialService.list_credentials
    │
    └─ POST /external/credentials/materialize  (selected ids)
            └─ DesktopCredentialService.materialize
                    ├─ local_access_refusal (access → category → consent → type)
                    ├─ CredentialsService.credential_to_env_dict / prepare_credentials_for_environment
                    │       (same shaping cloud environment sync uses)
                    └─ security event: CREDENTIAL_MATERIALIZED_LOCAL (requester, + owner if shared)
                            │
                            ▼
            Local agent's credentials/credentials.json
                    │
                    ▼
            scripts/cinna_credentials.py (Local Agent Kit) reads it the same way a cloud agent does
```

## Integration Points

- [Agent Credentials](../../agents/agent_credentials/agent_credentials.md) — reuses the same
  `credential_to_env_dict` / `prepare_credentials_for_environment` shaping and whitelist that cloud
  environment sync uses, so a delivered entry has the same shape as one written into a running
  container.
- [Credential Sharing](../../agents/agent_credentials/credential_sharing.md) — a shared credential
  is only ever eligible once it is both shared (`allow_sharing`) and the owner has separately
  turned on local-use consent (`allow_local_use`); revoking either stops future delivery.
- [Credential Security Hardening](../../agents/agent_credentials/credential_security_hardening.md) —
  `CREDENTIAL_MATERIALIZED_LOCAL` is the audit event this feature adds, fired for the requester and,
  for a shared credential, for the owner.
- [Desktop App Authentication](../desktop_auth/desktop_auth.md) — materialization requires a live,
  interactive Desktop client and is refused for a CLI-exchanged session.
- [Local Agent Kit](../local_agent_kit/local_agent_kit.md) — the delivery target: a materialized
  entry lands in a local agent's `credentials/credentials.json`, read by the same
  `scripts/cinna_credentials.py` a cloud agent uses, including its `unavailable_reason` handling
  for a refused credential.
