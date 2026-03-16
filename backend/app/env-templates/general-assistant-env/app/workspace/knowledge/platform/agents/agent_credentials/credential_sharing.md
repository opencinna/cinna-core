# Credential Sharing

## Purpose

Enables users to share their credentials with other users, allowing recipients to use shared credentials in their agents without exposing the actual credential values (passwords, tokens, etc.).

## Core Concepts

- **Credential Share** - Association record granting a recipient read-only access to another user's credential
- **Shareable Credential** - A credential with `allow_sharing=true`, enabling the owner to share it
- **Share Recipient** - User who received a credential share; can link it to their own agents but cannot view credential values
- **Access Level** - Currently only `read` access; recipients can use but not modify or view sensitive data

## User Stories / Flows

### Sharing a Credential

1. Credential owner enables sharing on a credential (`allow_sharing=true`)
2. Owner shares credential with another user by entering their email
3. Recipient sees credential in "Shared with Me" section of the Credentials page
4. Recipient can link shared credential to their agents via the Agent Credentials tab
5. Shared credential functions identically to owned credentials in agent environments

### Revoking Access

1. Owner revokes a specific share from the credential's sharing management panel
2. Alternatively, owner disables sharing entirely (`allow_sharing=false`)
3. Disabling sharing immediately revokes ALL existing shares (destructive, with confirmation)
4. Deleting a credential cascades to delete all its shares

## Business Rules

### Sharing States

| State | Description | Transitions |
|-------|-------------|-------------|
| `allow_sharing=false` | Credential cannot be shared | Enable sharing |
| `allow_sharing=true` | Credential can be shared with users | Share with user, Disable sharing |
| Shared | CredentialShare record exists for a recipient | Revoke share |

### Constraints

- Cannot share credentials with `allow_sharing=false`
- Cannot share with yourself
- Cannot create duplicate shares (same credential + same user)
- Cannot share with non-existent users
- Disabling `allow_sharing` immediately revokes ALL existing shares
- Deleting credential cascades to delete all shares

### Access Model

- **Owner** - Full control: view/edit/delete credential, manage shares, see credential values
- **Recipient** - Read-only: view metadata, link to own agents, use in environments; cannot see values, edit, or delete
- Credential values (`encrypted_data`) are never exposed to share recipients
- Revoking a share immediately removes the recipient's access

## Architecture Overview

```
Owner enables sharing → Shares credential by recipient email
         │
         └→ CredentialShare record created
                    │
                    ├→ Recipient sees in "Shared with Me" UI section
                    ├→ Recipient links shared credential to their agents
                    └→ Agent environments receive shared credential data (same as owned)

Owner revokes share → CredentialShare record deleted → Immediate access removal
```

## Integration Points

- [Agent Credentials](agent_credentials.md) - Shared credentials link to agents and sync to environments identically to owned credentials
- [Agent Sharing](../agent_sharing/agent_sharing.md) - Credential sharing is Phase 1 of the Shared Agents implementation; shared agents use owner's shared credentials
- [User Workspaces](../../application/user_workspaces/user_workspaces.md) - Credentials exist within workspace context
