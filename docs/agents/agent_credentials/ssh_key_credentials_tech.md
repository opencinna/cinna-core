# SSH Key Credentials — Technical Details

## File Locations

### Shared Crypto Util (new)
- `backend/app/core/ssh_key_utils.py` — generation, fingerprinting, validation, type detection. Used by both `CredentialsService` (agent SSH key credentials) and `SSHKeyService` (user SSH keys).

### Backend — Models
- `backend/app/models/credentials/credential.py` — `CredentialType.SSH_KEY` enum value; `SSHKeyCredentialData` validation model

### Backend — Services
- `backend/app/services/credentials/credentials_service.py` — SSH key processing (`process_ssh_key_credential_input`, `prepare_ssh_key_update_data`, `_generate_ssh_key_pair`, `_import_ssh_key_pair`, `_process_ssh_key_for_env`); extended `prepare_credentials_for_environment` that collects the `ssh_keys` sibling payload; `AGENT_ENV_ALLOWED_FIELDS` and `SENSITIVE_FIELDS` entries for `ssh_key`

### Backend — Routes
- `backend/app/api/routes/credentials.py` — type-branch validation on `POST /api/v1/credentials` and `PATCH /api/v1/credentials/{id}` that delegates to the service helpers above

### Agent Environment (inside container)
- `backend/app/env-templates/app_core_base/core/server/models.py` — `CredentialsUpdate` model with `ssh_keys: list[dict] | None = None`
- `backend/app/env-templates/app_core_base/core/server/routes.py` — `POST /config/credentials` handler; passes `credentials.ssh_keys` to `agent_env_service.update_credentials()`
- `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` — `update_ssh_keys()`, `_build_ssh_config()`, `_SSH_KEY_PREFIX`, `_SSH_KEY_UUID_RE`, `_KNOWN_HOSTS_SEED`

### Dockerfiles (updated)
- `backend/app/env-templates/general-env/Dockerfile`
- `backend/app/env-templates/general-assistant-env/Dockerfile`
- `backend/app/env-templates/python-env-advanced/Dockerfile`

All three add `openssh-client` to the apt-install list, providing `ssh`, `ssh-keyscan`, and `scp` to containers.

### Database Migration
- `backend/app/alembic/versions/f8a91b2c3e4d_add_ssh_key_credential_type.py`

### Frontend
- `frontend/src/components/Credentials/AddCredential.tsx` — two-step dialog flow (basic step → SSH key form step → success state); `sshKeyMutation` with cache seeding
- `frontend/src/components/Credentials/CredentialFields/SSHKeyFields.tsx` — create-time form (mode toggle, key type selector, import textareas, host aliases input, security disclaimer)
- `frontend/src/components/Credentials/CredentialForms/SSHKeyEditView.tsx` — edit page view (metadata left column, read-only key material + rotate right column)
- `frontend/src/components/Credentials/EditCredential.tsx` — routes `credential.type === "ssh_key"` to `SSHKeyEditView`
- `frontend/src/components/Agents/AgentCredentialsTab.tsx` — `getCredentialTypeLabel` returns "SSH Key" for type badge
- `frontend/src/components/Credentials/columns.tsx` — "SSH Key" label in list column mapping

---

## Data Model

### `CredentialType` Enum (extended)

File: `backend/app/models/credentials/credential.py`

```python
class CredentialType(str, Enum):
    ...
    SSH_KEY = "ssh_key"
```

The PostgreSQL enum stores member **names** (uppercase), consistent with existing members. The migration adds the value `'SSH_KEY'` via `ALTER TYPE credentialtype ADD VALUE IF NOT EXISTS 'SSH_KEY'`.

### `SSHKeyCredentialData` Validation Model

File: `backend/app/models/credentials/credential.py`

```python
class SSHKeyCredentialData(SQLModel):
    public_key: str
    private_key: str
    fingerprint: str
    key_type: str        # "rsa" | "ed25519" | "ecdsa" | "dss"
    passphrase: str | None = None
    host_aliases: list[str] | None = None
```

This model is not persisted as a table — it documents the normalised shape that is Fernet-encrypted and stored in `Credential.encrypted_data`.

### Stored Blob

The full `SSHKeyCredentialData` shape is what gets encrypted. Nothing is split across columns.

### No New Tables

The feature reuses the existing `credential` table, `AgentCredentialLink`, and `CredentialShare`. No migration for new tables is required.

---

## Database Migration

File: `backend/app/alembic/versions/f8a91b2c3e4d_add_ssh_key_credential_type.py`

```python
def upgrade():
    op.execute("ALTER TYPE credentialtype ADD VALUE IF NOT EXISTS 'SSH_KEY'")

def downgrade():
    pass  # PostgreSQL does not support removing enum values
```

Parent revision: `d40c20201e5b`.

---

## Shared Crypto Utilities

File: `backend/app/core/ssh_key_utils.py`

Extracted from `SSHKeyService` (user SSH keys) to eliminate duplication. No behavior change for the user SSH keys feature.

| Function | Signature | Purpose |
|----------|-----------|---------|
| `generate_rsa_key_pair` | `(name: str, key_size: int = 4096) -> tuple[str, str]` | Returns `(public_key_openssh, private_key_pem)`. Private key in TraditionalOpenSSL PEM format. |
| `generate_ed25519_key_pair` | `(name: str) -> tuple[str, str]` | Returns `(public_key_openssh, private_key_pem)`. Private key in OpenSSH PEM format (TraditionalOpenSSL does not support Ed25519). |
| `calculate_fingerprint` | `(public_key_str: str) -> str` | SHA256 fingerprint matching `ssh-keygen -lf` output: `SHA256:<base64-no-padding>`. Never raises (falls back to hashing the full string). |
| `detect_key_type` | `(public_key: str) -> str` | Returns `rsa` / `ed25519` / `ecdsa` / `dss` from the public key prefix. Defaults to `rsa` for unknown prefixes. |
| `validate_key_pair` | `(public_key: str, private_key: str) -> None` | Structural validation only — checks prefix and PEM markers. Does NOT verify the two halves match cryptographically. Raises `ValueError` with field-specific message on failure. |
| `is_private_key_encrypted` | `(private_key: str) -> bool` | Detects passphrase-encrypted keys (both `DEK-Info` style and OpenSSH container style). Used by `_import_ssh_key_pair` to reject encrypted imports in MVP. |

The `name` argument to key-generation functions is appended as the public key comment (spaces replaced with underscores). Falls back to `"cinna"` if empty.

---

## Service Layer

File: `backend/app/services/credentials/credentials_service.py`

### `CredentialsService.AGENT_ENV_ALLOWED_FIELDS` (extended)

```python
"ssh_key": ["public_key", "fingerprint", "key_type", "host_aliases"],
```

`private_key` and `passphrase` are excluded. They travel on the sibling `ssh_keys` array, not through `credentials.json`.

### `CredentialsService.SENSITIVE_FIELDS` (extended)

```python
"ssh_key": ["private_key", "passphrase"],
```

Belt-and-suspenders: even if a bug caused these fields to reach the README render path, they would still appear as `***REDACTED***`.

### SSH key processing methods

**`process_ssh_key_credential_input(raw_data: dict, credential_name: str | None = None) -> dict`**

Entry point for the create path (and rotation via the update path). Dispatches to `_generate_ssh_key_pair` or `_import_ssh_key_pair` based on `raw_data["mode"]`.

Raises `ValueError` (converted to HTTP 422 by the route) for:
- Missing or invalid `mode`
- Unknown `key_type` in generate mode (only `rsa` / `ed25519`)
- Invalid public key prefix or missing PEM markers in import mode
- Passphrase-encrypted private key in import mode
- Invalid `host_aliases` (non-list, non-string values, or characters outside `[A-Za-z0-9_.\-*?\[\]]+`)

Host aliases are normalised: trimmed, deduplicated (order-preserving), and validated against `_SSH_HOST_ALIAS_RE`. A list that reduces to empty after normalisation is stored as `None` (meaning "all hosts").

**`prepare_ssh_key_update_data(session, credential, raw_data, credential_name) -> dict`**

Entry point for the update/rotate path.

- If `mode` key is present in `raw_data`: delegates to `process_ssh_key_credential_input` (full key rotation or re-import).
- If `mode` is absent: metadata-only update. Only `host_aliases` may be changed. Any other field in `raw_data` raises `ValueError`. The existing blob is decrypted and the `host_aliases` value is merged in.

**`_generate_ssh_key_pair(key_type, name, host_aliases) -> dict`**

Wraps `generate_rsa_key_pair` or `generate_ed25519_key_pair`, computes fingerprint via `calculate_fingerprint`, detects key type via `detect_key_type`. Returns the full normalised blob.

**`_import_ssh_key_pair(public_key, private_key, passphrase, host_aliases) -> dict`**

Trims inputs, runs `validate_key_pair`, rejects encrypted private keys via `is_private_key_encrypted`. Computes fingerprint and key type. Returns normalised blob. `passphrase` is always stored as `None` in MVP.

**`_process_ssh_key_for_env(credential_data: dict) -> dict`**

Strips private key material and returns only the whitelist-safe surface for `credentials.json`:

```python
{
    "public_key": ...,
    "fingerprint": ...,
    "key_type": ...,
    "host_aliases": credential_data.get("host_aliases") or ["*"],
}
```

`host_aliases` defaults to `["*"]` at this step (the env-core side also defaults to `["*"]` when it builds `~/.ssh/config`).

### `prepare_credentials_for_environment` (extended)

Before the standard whitelist filter runs, this method now also:

1. Iterates credentials for `type == "ssh_key"` and appends each to `ssh_keys: list[dict]`:
   ```python
   {"credential_id": ..., "private_key": ..., "public_key": ..., "passphrase": ..., "host_aliases": ...}
   ```
2. Replaces `cred["credential_data"]` with the output of `_process_ssh_key_for_env()` so the downstream whitelist filter sees the already-stripped metadata.
3. Returns `"ssh_keys": ssh_keys` as a new key alongside `"credentials_json"`, `"credentials_readme"`, and `"service_account_files"`.

This design keeps private key material off the `credentials.json` / README path entirely rather than relying solely on the whitelist filter.

---

## API Routes

All SSH key operations reuse the existing `/api/v1/credentials` endpoints. No new routes were added.

File: `backend/app/api/routes/credentials.py`

### `POST /api/v1/credentials`

If `credential_in.type == CredentialType.SSH_KEY`:

```python
credential_in.credential_data = CredentialsService.process_ssh_key_credential_input(
    credential_in.credential_data or {},
    credential_name=credential_in.name,
)
```

`ValueError` from the service becomes HTTP 422.

The create handler returns `CredentialWithData` (not just `CredentialPublic`) so the frontend receives the decrypted `credential_data` — including `public_key` and `fingerprint` — to display in the success state.

### `PATCH /api/v1/credentials/{id}`

If `credential.type == CredentialType.SSH_KEY` and `credential_in.credential_data` is provided:

```python
credential_in.credential_data = CredentialsService.prepare_ssh_key_update_data(
    session=session,
    credential=credential,
    raw_data=credential_in.credential_data,
    credential_name=credential_in.name,
)
```

`ValueError` becomes HTTP 422.

---

## Agent-Environment Side

### `CredentialsUpdate` model

File: `backend/app/env-templates/app_core_base/core/server/models.py`

```python
class CredentialsUpdate(BaseModel):
    ...
    ssh_keys: list[dict] | None = None
```

The `POST /config/credentials` route passes `credentials.ssh_keys` to `agent_env_service.update_credentials(ssh_keys=...)`.

### `AgentEnvService.update_ssh_keys(ssh_keys: list[dict]) -> list[str]`

File: `backend/app/env-templates/app_core_base/core/server/agent_env_service.py`

Runs synchronously within the `POST /config/credentials` request. All steps are idempotent.

**Step 1 — ensure `~/.ssh/` exists at mode 0700**

`Path.mkdir(parents=True, exist_ok=True)` followed by `os.chmod(0o700)` (mkdir may honour a tighter umask).

**Step 2 — seed `known_hosts`**

Only written if `~/.ssh/known_hosts` is missing or does not contain a `github.com` entry. This preserves user-added entries (TOFU handles new hosts via `StrictHostKeyChecking accept-new`). Constant: `_KNOWN_HOSTS_SEED` in the module.

Providers in the seed: `github.com`, `gitlab.com`, `bitbucket.org` (Ed25519 + ECDSA + RSA fingerprints for each).

**Step 3 — write key files**

For each entry in `ssh_keys`:
- `~/.ssh/id_<credential_id>` — private key, trailing newline enforced, mode 0600
- `~/.ssh/id_<credential_id>.pub` — public key, trailing newline enforced, mode 0644

Entries with missing `credential_id`, `private_key`, or `public_key` are skipped with a warning.

**Step 4 — regenerate `~/.ssh/config`**

Delegates to `_build_ssh_config()`. Written to `~/.ssh/config` at mode 0600.

**Step 5 — orphan reconciliation**

Iterates `~/.ssh/id_*` and `~/.ssh/id_*.pub`. For each:
1. Strip `_SSH_KEY_PREFIX` (`"id_"`) and any `.pub` suffix to get the stem.
2. Match stem against `_SSH_KEY_UUID_RE` (`^[0-9a-f]{8}-...-[0-9a-f]{12}$`). Non-UUID stems are skipped (protects `id_rsa`, `id_ed25519`, etc.).
3. If the UUID is not in `synced_ids`, delete the file.

Returns a list of relative paths that were written or deleted (for logging).

### `AgentEnvService._build_ssh_config(ssh_keys, synced_ids, ssh_dir) -> str`

Two categories of key:
- **Global keys** (`host_aliases` is empty, `None`, or `["*"]`): placed under the catch-all `Host *` block as `IdentityFile` lines with `IdentitiesOnly no`.
- **Scoped keys** (specific host list): get their own `Host <aliases>` stanza before the catch-all, with `IdentityFile` and `IdentitiesOnly yes`.

Scoped entries are written first so OpenSSH evaluates them before the catch-all.

Global defaults written under `Host *`:
```
StrictHostKeyChecking accept-new
UserKnownHostsFile ~/.ssh/known_hosts
ServerAliveInterval 60
IdentitiesOnly no
```

### `_SSH_KEY_PREFIX` and `_SSH_KEY_UUID_RE`

```python
_SSH_KEY_PREFIX = "id_"
_SSH_KEY_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
```

The prefix constant is a class attribute on `AgentEnvService`.

---

## Frontend

### `AddCredential.tsx` — Two-Step Dialog Flow

The credential dialog uses a local state `step: "basic" | "ssh_key" | "ssh_key_success"`.

1. **`basic` step**: name + type dropdown. For all types except `ssh_key`, clicking "Save" calls `basicMutation` (creates a skeleton credential record). For `ssh_key`, clicking "Next" advances to the `ssh_key` step without creating anything yet (the backend requires `credential_data` at creation time).
2. **`ssh_key` step**: renders `SSHKeyFields`. Submitting calls `sshKeyMutation` with the full `credential_data` payload.
3. **`ssh_key_success` step**: shows the returned `public_key` and `fingerprint` with copy buttons. Clicking "Done" navigates to the credential detail page at `/credential/<id>`. The query client cache is seeded with the creation response so the detail page renders instantly.

`sshKeyMutation` posts to `CredentialsService.createCredential` from the auto-generated client. On success, it calls `queryClient.setQueryData(["credential-with-data", id], responseData)` and `queryClient.invalidateQueries(["credentials"])`.

### `SSHKeyFields.tsx` — Create Form

A two-column layout driven by `react-hook-form`.

Left column: name, notes (textarea), security disclaimer alert.

Right column:
- Mode toggle (Tabs: "Generate new key" / "Import existing key") mapped to `credential_data.mode`
- Generate mode: key type Select (`ed25519` default, `rsa`)
- Import mode: public key textarea, private key textarea
- Host aliases text input (comma-separated, mapped to `credential_data.host_aliases_text`); the `onSshKeySubmit` handler splits this string into a `string[]` before calling the API

### `SSHKeyEditView.tsx` — Credential Detail / Edit

Two-column layout on the credential detail page:

Left column: `name`, `notes`, `host_aliases_text` (editable). Shows Save/Reset only when the form is dirty. Calls `CredentialsService.updateCredential` with `credential_data: { host_aliases: ... }` (metadata-only update, no `mode`).

Right column: `key_type` (read-only Input), `public_key` (read-only Textarea with copy button), `fingerprint` (read-only Input with copy button), and the "Rotate Key" AlertDialog.

**Rotate flow**: `rotateMutation` calls `CredentialsService.updateCredential` with:
```json
{ "credential_data": { "mode": "generate", "key_type": "<preserved>", "host_aliases": [...] } }
```
The Rotate button is disabled while the metadata form has unsaved changes (`form.formState.isDirty`). On success the query cache is invalidated so the right column updates with the new key material.

`aliasesToText` / `parseHostAliases` are local helpers for converting between `string[]` (API) and comma-separated string (UI input).

### Type Badge in List Views

`frontend/src/components/Credentials/columns.tsx`: `"ssh_key": "SSH Key"` in the type-label map.
`frontend/src/components/Agents/AgentCredentialsTab.tsx`: `getCredentialTypeLabel` returns "SSH Key" for `case "ssh_key"`.

---

## Configuration

No new environment variables or feature flags.

The `is_private_key_encrypted` check in `ssh_key_utils.py` uses the `cryptography` library (already a dependency). Generation uses `cryptography.hazmat.primitives.asymmetric.rsa` and `ed25519`.

---

## Related Docs

- [SSH Key Credentials](ssh_key_credentials.md) — purpose, user flows, security model summary
- [Agent Credentials](agent_credentials.md) — parent feature: credential lifecycle, sync rules, redaction
- [Agent Credentials Tech](agent_credentials_tech.md) — parent tech doc: file locations, core services, API
- [Credentials Whitelist](credentials_whitelist.md) — three-layer security model, per-type field lists
- [Google Service Account](google_service_account.md) — analogous encrypted-blob credential with separate file delivery (SA JSON vs SSH private key)
- [User SSH Keys Tech](../../application/ssh_keys/ssh_keys_tech.md) — `SSHKeyService`, which now delegates to `ssh_key_utils.py`
