# SSH Keys

## Purpose

Allows users to securely store SSH key pairs for authenticating with private Git repositories. Keys are used by the Knowledge Sources feature to clone and sync documentation repositories that require SSH access.

## Core Concepts

- **SSH Key** - RSA/Ed25519/ECDSA key pair stored encrypted in the database. Only the public key and fingerprint are exposed via API
- **Fingerprint** - SHA256 hash of the public key, used for deduplication and identification (format: `SHA256:...`)
- **Key Generation** - Platform generates RSA 4096-bit keys server-side and returns the public key for the user to add to their Git provider
- **Key Import** - Users can import existing key pairs (public + private + optional passphrase)

## User Stories / Flows

### Generate a New Key

1. User navigates to Settings > SSH Keys tab
2. Clicks "Generate Key", enters a descriptive name
3. System generates RSA 4096-bit key pair, encrypts private key, stores in database
4. Modal displays the public key for the user to copy
5. User adds the public key to their Git provider (GitHub, GitLab, etc.)

### Import an Existing Key

1. User navigates to Settings > SSH Keys tab
2. Clicks "Import Key"
3. Pastes name, public key, private key, and optional passphrase
4. System validates key format, encrypts private key and passphrase, stores in database
5. Key appears in the list with calculated fingerprint

### Edit / Delete a Key

1. User clicks edit on a key row to rename it (public/private key cannot be changed)
2. User clicks delete to remove a key
3. If the key is referenced by knowledge sources, those sources become `disconnected`

## Business Rules

- **Ownership**: Keys are strictly user-owned. All operations verify `user_id` matches the authenticated user
- **Deduplication**: Duplicate keys (same fingerprint) are rejected for the same user
- **Encryption at rest**: Private keys and passphrases are encrypted with Fernet (AES) using the application's `ENCRYPTION_KEY`
- **No export**: Private keys cannot be viewed or exported after storage. Only the public key is readable
- **Cascade on delete**: Deleting a user cascades to delete all their SSH keys (FK constraint)
- **Knowledge source impact**: Deleting an SSH key referenced by knowledge sources causes those sources to lose access (status transitions to `disconnected`)

### Supported Key Types

- `ssh-rsa` (RSA)
- `ssh-ed25519` (EdDSA)
- `ssh-dss` (DSA)
- `ecdsa-sha2-*` (ECDSA)

### Validation Rules

- **Name**: 1-255 characters, required
- **Public key**: Must start with a recognized key type prefix
- **Private key**: Must contain PEM `BEGIN` and `PRIVATE KEY` markers
- **Passphrase**: Optional, only needed for encrypted private keys

## Architecture Overview

```
User (Settings Page) --> Frontend (SSHKeys component) --> Backend API (/api/v1/ssh-keys)
                                                              |
                                                         SSHKeyService
                                                              |
                                                    encrypt_field() / decrypt_field()
                                                              |
                                                     PostgreSQL (user_ssh_keys)
```

Decryption only occurs when Git operations need the private key (in-memory, never logged).

## Integration Points

- **Knowledge Sources** - SSH keys are referenced by knowledge sources (`ssh_key_id` FK) for private repository access. See [Knowledge Sources](../knowledge_sources/knowledge_sources.md)
- **Plugin Marketplaces** - Admins can assign SSH keys to private marketplace Git repos, using the same mechanism as knowledge sources. See [Plugin Marketplaces](../plugin_marketplaces/plugin_marketplaces.md)
- **Git Operations** - `SSHKeyService.get_decrypted_private_key()` provides decrypted keys to `git_operations.py` for cloning and verifying repositories
- **Security module** - Uses `backend/app/core/security.py` shared `encrypt_field()` / `decrypt_field()` functions (Fernet/PBKDF2)

## Security

- Private keys encrypted with Fernet (AES-128-CBC + HMAC-SHA256) via PBKDF2 key derivation (100,000 iterations)
- Encryption key sourced from `ENCRYPTION_KEY` environment variable
- Private keys never included in API responses (`SSHKeyPublic` model excludes encrypted fields)
- Decrypted keys exist only in-memory during Git operations, written to temp files with `0o600` permissions, auto-cleaned in `finally` blocks
- `StrictHostKeyChecking=no` used for SSH connections (ease of use for various Git hosts)
