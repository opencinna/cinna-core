# SSH Keys - Technical Details

## File Locations

### Backend

- **Model**: `backend/app/models/users/ssh_key.py` - `UserSSHKey` (table), `SSHKeyBase`, `SSHKeyPublic`, `SSHKeysPublic`, `SSHKeyGenerate`, `SSHKeyImport`, `SSHKeyUpdate`
- **Routes**: `backend/app/api/routes/ssh_keys.py` - 6 endpoints under `/api/v1/ssh-keys`
- **Service**: `backend/app/services/users/ssh_key_service.py` - `SSHKeyService` (static methods)
- **Security**: `backend/app/core/security.py` - `encrypt_field()`, `decrypt_field()`, Fernet cipher setup
- **Migration**: `backend/app/alembic/versions/dcbfc8267939_add_user_ssh_keys_table.py`

### Frontend

- **Main component**: `frontend/src/components/UserSettings/SSHKeys.tsx` - Key list with table, actions, delete confirmation
- **Generate modal**: `frontend/src/components/UserSettings/GenerateKeyModal.tsx` - Two-state UI (input form / success display with public key)
- **Import modal**: `frontend/src/components/UserSettings/ImportKeyModal.tsx` - Form with name, public key, private key, passphrase fields
- **Edit modal**: `frontend/src/components/UserSettings/EditKeyModal.tsx` - Rename key, view public key (read-only), view fingerprint
- **Settings page**: `frontend/src/routes/_layout/settings.tsx` - Contains SSH Keys tab
- **API client**: `frontend/src/client/sdk.gen.ts` - `SshKeysService`

## Database Schema

### Table: `user_ssh_keys`

| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| user_id | UUID | FK -> user(id) ON DELETE CASCADE, NOT NULL |
| name | VARCHAR(255) | NOT NULL |
| public_key | TEXT | NOT NULL |
| private_key_encrypted | TEXT | NOT NULL |
| passphrase_encrypted | TEXT | nullable |
| fingerprint | VARCHAR(255) | NOT NULL, indexed (`ix_user_ssh_keys_fingerprint`) |
| created_at | DATETIME | NOT NULL |
| updated_at | DATETIME | NOT NULL |

Migration: `backend/app/alembic/versions/dcbfc8267939_add_user_ssh_keys_table.py`

## API Endpoints

**Route file**: `backend/app/api/routes/ssh_keys.py`
**Prefix**: `/api/v1/ssh-keys` | **Tag**: `ssh-keys`

| Method | Path | Description | Request | Response |
|--------|------|-------------|---------|----------|
| GET | `/` | List user's SSH keys | - | `SSHKeysPublic` |
| GET | `/{id}` | Get single SSH key | - | `SSHKeyPublic` |
| POST | `/generate` | Generate RSA 4096-bit key pair | `SSHKeyGenerate` | `SSHKeyPublic` |
| POST | `/` | Import existing key pair | `SSHKeyImport` | `SSHKeyPublic` |
| PUT | `/{id}` | Update key name | `SSHKeyUpdate` | `SSHKeyPublic` |
| DELETE | `/{id}` | Delete SSH key | - | `Message` |

All endpoints require `CurrentUser` authentication. GET/PUT/DELETE verify ownership.

## Services & Key Methods

### `backend/app/services/users/ssh_key_service.py` - SSHKeyService

| Method | Purpose |
|--------|---------|
| `generate_key_pair(session, user_id, data)` | Generates RSA 4096-bit key, encrypts private key, calculates fingerprint, checks duplicates, stores in DB |
| `import_key(session, user_id, data)` | Validates key format, encrypts private key + passphrase, calculates fingerprint, checks duplicates |
| `get_user_keys(session, user_id)` | Lists all keys for user, ordered by `created_at` descending |
| `get_key_by_id(session, key_id, user_id)` | Gets key with ownership verification |
| `update_key(session, key_id, user_id, data)` | Updates name only, verifies ownership |
| `delete_key(session, key_id, user_id)` | Deletes key with ownership verification |
| `get_decrypted_private_key(session, key_id, user_id)` | Returns `(private_key, passphrase)` tuple for Git operations. Never exposed via API |
| `_calculate_fingerprint(public_key_str)` | SHA256 fingerprint in `SHA256:...` format (base64, no padding) |
| `_validate_ssh_key_format(public_key, private_key)` | Validates public key prefix and private key PEM markers |

### `backend/app/core/security.py` - Encryption

| Function | Purpose |
|----------|---------|
| `encrypt_field(value)` | Fernet encryption using PBKDF2-derived key from `ENCRYPTION_KEY` setting |
| `decrypt_field(encrypted_value)` | Fernet decryption, returns empty string for empty input |

Key derivation: PBKDF2-HMAC-SHA256, 100,000 iterations, static salt `"credentials_salt"`

## Frontend Components

### `frontend/src/components/UserSettings/SSHKeys.tsx`

- Renders Card with "SSH Keys" header
- "Generate Key" and "Import Key" buttons
- Table: Name, Fingerprint (truncated to 30 chars), Created date, Actions (copy/edit/delete)
- AlertDialog for delete confirmation
- Query key: `["sshKeys"]`

### `frontend/src/components/UserSettings/GenerateKeyModal.tsx`

- Two-state dialog: input form -> success display
- Success state shows public key in textarea with copy button, fingerprint read-only
- Invalidates `["sshKeys"]` query on success

### `frontend/src/components/UserSettings/ImportKeyModal.tsx`

- Form: name, public key (textarea), private key (textarea), passphrase (password input, optional)
- Monospace textareas for key inputs
- Invalidates `["sshKeys"]` query on success

### `frontend/src/components/UserSettings/EditKeyModal.tsx`

- Editable: name only
- Read-only: public key (with copy button), fingerprint
- Security note about private key not being viewable/exportable
- Skips API call if name unchanged

## Configuration

| Setting | Source | Purpose |
|---------|--------|---------|
| `ENCRYPTION_KEY` | `.env` | Base key for Fernet encryption of private keys and passphrases |

## Security

- **API response model** (`SSHKeyPublic`): Explicitly excludes `private_key_encrypted` and `passphrase_encrypted` fields
- **Ownership enforcement**: Every service method checks `user_id` before returning or modifying data
- **Fingerprint deduplication**: Prevents duplicate key imports per user
- **Key generation**: RSA 4096-bit, PEM format (Traditional OpenSSL), OpenSSH public key format with name as comment
- **Decryption path**: Only `get_decrypted_private_key()` -> called by `KnowledgeSourceService` for Git operations -> temp file with `0o600` -> cleanup in `finally`
