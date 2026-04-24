# SSH Key Credentials

## Purpose

Lets users store an SSH key pair as a credential and have that key automatically materialized inside agent containers — so `git clone git@github.com:org/repo`, `ssh`, and any other tool that reads `~/.ssh/config` work out-of-the-box without extra setup.

The key pair can be server-generated (RSA 4096 or Ed25519) or imported from an existing pair. Either way, the private key is encrypted at rest and is never exposed through `credentials.json` or the agent prompt README. Only public metadata (public key, fingerprint, key type, host aliases) is forwarded to the workspace files.

## Not to Be Confused With: User SSH Keys

There are two distinct SSH key features in this platform — do not confuse them:

| | [User SSH Keys](../../application/ssh_keys/ssh_keys.md) | Agent SSH Key Credentials |
|-|----------------------------------------------------------|---------------------------|
| Where stored | `user_ssh_keys` table, per-user | `credential` table, per-workspace |
| Who uses them | Backend server only | Agent containers |
| Purpose | Backend-side Git ops for Knowledge Sources and Plugin Marketplaces | SSH access from inside agent containers |
| Shared/linked | Not shareable | Shareable via `CredentialShare`, linkable to multiple agents |
| Visible to agents | No | Public key + fingerprint in `credentials.json`; private key in `~/.ssh/` |

Choose agent SSH key credentials when you want an agent to run `git clone git@…`, `scp`, or any `ssh` command itself. Choose user SSH keys when you need the backend to pull a private Git repo for a Knowledge Source or Plugin Marketplace.

## Core Concepts

- **Generate mode** — server generates the key pair; user never sees the private key in the browser
- **Import mode** — user supplies an existing public + private key pair (unencrypted keys only in MVP)
- **Host aliases** — optional list of hostnames this key should bind to (e.g., `["github.com", "gitlab.com"]`); empty means "all hosts"
- **Fingerprint** — SHA256 hash of the public key, displayed for easy verification against Git provider deploy-key pages
- **Orphan reconciliation** — on every credential sync, keys for unlinked/deleted credentials are removed from `~/.ssh/`

## User Flows

### Flow 1 — Generate and Deploy

1. User opens Credentials, clicks "Add Credential", selects "SSH Key".
2. A two-step dialog opens. On the basic step the user enters a name and confirms the type; clicking Next opens the SSH key form.
3. User selects "Generate new key", picks Ed25519 (recommended) or RSA 4096, optionally enters host aliases, and clicks Create.
4. Backend generates the key pair, encrypts and stores it, returns public key + fingerprint.
5. Success state shows the public key in a read-only textarea with a copy button and the fingerprint. The user copies the public key and adds it as a deploy key on GitHub/GitLab/etc.
6. User navigates to the agent, opens the Credentials tab, and checks the new SSH Key credential.
7. If the environment is running, it auto-syncs; the private key is written to `/root/.ssh/id_<uuid>` (mode 0600) and `~/.ssh/config` is regenerated.
8. User in chat: "Clone `git@github.com:org/repo` into `/app/workspace/files/repositories/repo`."
9. Agent runs `git clone git@github.com:org/repo ...` — succeeds without interactive prompts.

### Flow 2 — Import an Existing Key Pair

1. User opens the SSH Key credential form and selects "Import existing key".
2. Pastes the public key and the private key (PEM-encoded, unencrypted).
3. Backend validates format, computes fingerprint, encrypts, stores.
4. User links to one or more agents — same sync path as generate mode.

**MVP constraint**: Passphrase-encrypted private keys are rejected on import. The server returns HTTP 422 with the message "Encrypted private keys are not yet supported — please export without passphrase or generate a new key."

### Flow 3 — Rotate a Key

1. User opens the credential detail page.
2. Clicks "Rotate Key" (destructive-style button, requires no unsaved metadata edits — the button is disabled while the metadata form is dirty).
3. Confirmation dialog warns that the old key will stop working and deploy keys must be updated.
4. On confirm, the backend generates a fresh key pair of the same type, preserving `host_aliases`. The credential is updated and all linked environments re-sync on their next event.
5. Success state shows the new public key. User updates the deploy key on the Git provider.

### Flow 4 — Share with a Teammate

1. Owner enables `allow_sharing` on the credential.
2. Adds a teammate by email via the Share dialog (reuses existing `CredentialShare` UI).
3. Recipient sees the credential in "Shared with Me" and can link it to their own agents.
4. The recipient's agents receive the same private key on sync — the key is never exposed to the recipient directly. Only the public key and fingerprint are visible in `credentials.json`.

### Flow 5 — Metadata Update (Host Aliases)

1. User opens the credential detail page.
2. Updates the "Host Aliases" field (comma-separated, e.g., `github.com, gitlab.com`).
3. Saves. Backend validates and normalises the aliases (trims, deduplicates, rejects characters that would inject SSH config directives).
4. All linked environments re-sync; `~/.ssh/config` is regenerated with the updated host bindings.

## What the Agent Sees

After a sync, the agent environment contains:

```
~/.ssh/
├── id_<credential-uuid>          (0600, private key)
├── id_<credential-uuid>.pub      (0644, public key)
├── config                        (0600, managed by cinna)
└── known_hosts                   (0644, pre-seeded + TOFU for new hosts)

workspace/credentials/
├── credentials.json              (ssh_key entry: public_key, fingerprint, key_type, host_aliases)
└── README.md                     (same structure, private_key shown as ***REDACTED***)
```

Agents discover the public metadata via `credentials.json` and use the private key transparently through standard `git`/`ssh` tooling — no extra configuration needed.

### credentials.json entry (example)

```json
{
  "id": "6a32aeb0-3a26-43eb-ab2b-d9df720be807",
  "name": "GitHub deploy – Monorepo",
  "type": "ssh_key",
  "credential_data": {
    "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5... GitHub_deploy_Monorepo",
    "fingerprint": "SHA256:abc123...",
    "key_type": "ed25519",
    "host_aliases": ["github.com"]
  }
}
```

The `private_key` and `passphrase` fields are not present anywhere in the workspace files. They are delivered to the container over an authenticated in-container HTTP call and written directly to `~/.ssh/`.

## Pre-Seeded Known Hosts

`~/.ssh/known_hosts` is pre-seeded with verified fingerprints for the most common Git providers. This prevents interactive "Are you sure you want to continue connecting?" prompts on first use while maintaining strict verification for known hosts:

- `github.com` (Ed25519, ECDSA, RSA)
- `gitlab.com` (Ed25519, ECDSA, RSA)
- `bitbucket.org` (Ed25519, ECDSA, RSA)

Other hosts are handled via `StrictHostKeyChecking accept-new` in `~/.ssh/config` (trust-on-first-use). The known_hosts file is only seeded if it does not already contain a `github.com` entry — any custom entries the user has added are preserved.

`dev.azure.com` and `codeberg.org` are included in the plan as intended targets but were not present in the hardcoded seed at the time this feature shipped. The three providers above cover the most common use cases.

## Security Model

| Layer | What happens |
|-------|-------------|
| Encryption at rest | Entire blob (`public_key`, `private_key`, `passphrase`, `fingerprint`, `key_type`, `host_aliases`) is Fernet-encrypted in `Credential.encrypted_data` |
| Field whitelist (`credentials.json`) | Only `public_key`, `fingerprint`, `key_type`, `host_aliases` are written to `credentials.json`; `private_key` and `passphrase` are excluded |
| Prompt README redaction | Even if a future change accidentally included these fields, `private_key` and `passphrase` are in `SENSITIVE_FIELDS` and would render as `***REDACTED***` |
| Private key delivery | Transported over the authenticated `POST /config/credentials` call on the Docker bridge network (not published externally); written to `~/.ssh/id_<uuid>` at mode 0600 |
| SSH directory permissions | `~/.ssh/` is created/forced to mode 0700; config is 0600; private key files are 0600; public key files are 0644 |
| Host alias injection defence | `host_aliases` values are validated against a strict regex (`[A-Za-z0-9_.\-*?\[\]]+`) before being written into `~/.ssh/config` — prevents newline injection that could add rogue directives |
| Orphan reconciliation | On every sync, any `id_<uuid>` / `id_<uuid>.pub` file whose UUID is not in the current credential list is deleted. Only UUID-shaped stems are ever removed — user-placed files like `id_rsa` are untouched |

Private keys exist only in:
1. `Credential.encrypted_data` (Fernet-encrypted at rest in PostgreSQL)
2. The in-memory payload during `POST /config/credentials` (TLS-protected on the bridge network)
3. `~/.ssh/id_<uuid>` inside the container filesystem (0600, root-owned)

They are never logged, never in `credentials.json`, never in the README, and never returned by any API endpoint.

## MVP Constraints and Known Gaps

These are intentional limitations in the first release. They do not represent bugs.

| Constraint | Detail |
|------------|--------|
| No passphrase-encrypted import | Server rejects encrypted private keys on import with HTTP 422. Users must export without a passphrase or generate a new key. |
| No public/private match verification | Format is validated (prefix + PEM markers) but the two halves are not cryptographically verified to match each other. A mismatched pair will fail silently at `git clone` time. |
| No fingerprint deduplication | Unlike user SSH keys, duplicate fingerprints across credentials are allowed. A warning is logged but no UI error is shown. |
| `dev.azure.com` / `codeberg.org` not in seed | The plan listed five providers; only three (GitHub, GitLab, Bitbucket) are in the hardcoded `_KNOWN_HOSTS_SEED`. Azure DevOps and Codeberg rely on TOFU (trust-on-first-use). |
| No `Rotate` REST endpoint | Key rotation is performed by the frontend sending a `PATCH` with `mode=generate` in `credential_data`. A dedicated `POST /credentials/{id}/rotate-ssh-key` endpoint is listed as future work. |

## Auto-Sync Triggers

SSH key credentials follow the standard credential sync rules (see [Agent Credentials](agent_credentials.md)):

- Environment starts or rebuilds
- Credential created, updated, or deleted
- Credential linked or unlinked from an agent
- Credential rotation (treated as an update)

On every sync, `~/.ssh/config` is regenerated from scratch and orphan key files are removed. Sync errors are logged but do not block other environments.

## Integration Points

- [Agent Credentials](agent_credentials.md) — parent feature: credential lifecycle, encryption, whitelist model, sync triggers
- [Credentials Whitelist](credentials_whitelist.md) — three-layer security model; the `ssh_key` row in the whitelist table
- [Agent Environments](../agent_environments/agent_environments.md) — environment lifecycle triggers that drive credential sync
- [User SSH Keys](../../application/ssh_keys/ssh_keys.md) — the separate user-level SSH key feature for backend Git operations (Knowledge Sources, Plugin Marketplaces); shares `ssh_key_utils.py` crypto helpers but is otherwise independent
- [SSH Key Credentials Tech](ssh_key_credentials_tech.md) — models, service layer, agent-env implementation, frontend components
