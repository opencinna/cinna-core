# Credentials Management Feature Specification

**Version:** 1.0
**Last Updated:** 2025-12-22
**Status:** Implemented

## Overview

The Credentials Management feature provides users with a secure centralized repository for storing and managing authentication credentials required for integrating with external services. This feature enables users to safely store sensitive information such as email credentials, API tokens, and OAuth access tokens, which can later be used by workflow agents and automation scripts.

## Purpose

### Problem Statement

Workflow agents and automation scripts require credentials to interact with external services (email servers, third-party APIs, SaaS platforms). Hardcoding credentials in scripts or storing them in plain text creates security vulnerabilities and management challenges.

### Solution

A secure credential vault that:
- Stores credentials with encryption at rest
- Provides a unified interface for managing credentials across different service types
- Enforces user-level access control
- Supports multiple credential types with service-specific schemas
- Enables credential reuse across multiple workflows

## Architecture

### High-Level Components

```
┌─────────────────────────────────────────────────────────┐
│                    User Interface                       │
│  (CRUD operations, type-specific forms)                 │
└─────────────────┬───────────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────────┐
│              Business Logic Layer                       │
│  - Credential validation                                │
│  - Access control enforcement                           │
│  - Encryption/decryption orchestration                  │
└─────────────────┬───────────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────────┐
│              Security Layer                             │
│  - Field-level encryption (AES-256)                     │
│  - Key derivation (PBKDF2)                              │
│  - Secure key storage                                   │
└─────────────────┬───────────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────────┐
│              Data Persistence                           │
│  - Encrypted credential storage                         │
│  - User ownership association                           │
│  - Metadata (name, type, notes)                         │
└─────────────────────────────────────────────────────────┘
```

### Data Flow

**Credential Creation:**
1. User selects credential type and fills type-specific form
2. System validates input against credential type schema
3. Sensitive fields are encrypted before storage
4. Encrypted credential is associated with user account
5. Metadata is stored for credential discovery and management

**Credential Retrieval:**
1. User requests credential access
2. System verifies user ownership or admin privileges
3. For general listing: metadata returned without decryption
4. For usage: decryption performed on-demand
5. Decrypted data provided to authorized consumer (user or workflow)

**Credential Updates:**
1. User modifies credential data
2. System re-encrypts all sensitive fields
3. Updated encrypted data replaces previous version
4. Metadata updates are applied

**Credential Deletion:**
1. User requests deletion
2. System verifies ownership/permissions
3. Encrypted data is permanently removed from storage
4. Associated references are cleaned up (cascade delete)

## Credential Types

The system supports multiple credential types, each with a specific schema optimized for particular service integrations.

### 1. Email (IMAP) Credentials

**Use Case:** Accessing email servers for workflow automation, email monitoring, or automated responses.

**Schema:**
- **Host:** Email server hostname (e.g., imap.gmail.com)
- **Port:** IMAP port number (typically 993 for SSL)
- **Login:** Email account username/email address
- **Password:** Email account password or app-specific password
- **SSL Enabled:** Boolean flag for SSL/TLS encryption

**Security Considerations:**
- Password encrypted at rest
- Supports app-specific passwords for enhanced security
- SSL enforcement recommended

### 2. Odoo Credentials

**Use Case:** Integration with Odoo ERP/CRM systems for data synchronization, automated record management, or reporting.

**Schema:**
- **URL:** Odoo instance base URL
- **Database Name:** Target database identifier
- **Login:** Odoo username
- **API Token:** Odoo API authentication token

**Security Considerations:**
- API token encrypted at rest
- URL validation to prevent injection attacks
- Database name isolation per credential

### 3. Gmail OAuth Credentials

**Use Case:** Accessing Gmail via OAuth 2.0 for automated email processing, label management, or message operations with user consent.

**Schema:**
- **Access Token:** OAuth 2.0 access token
- **Refresh Token:** Token for renewing access token (optional)
- **Token Type:** Typically "Bearer"
- **Expires At:** Unix timestamp of token expiration (optional)
- **Scope:** OAuth scope permissions granted

**Security Considerations:**
- Both access and refresh tokens encrypted
- Token expiration tracking for automatic renewal
- Scope limitation enforcement

## Business Rules

### Ownership and Access Control

1. **User Ownership:**
   - Each credential belongs to a single user (the creator)
   - Only the owner can view, edit, or delete their credentials
   - Credentials are not shared between users by default

2. **Superuser Access:**
   - System administrators (superusers) can view all credentials
   - Superuser access enables system-wide credential management
   - Audit logging recommended for superuser access

3. **Isolation:**
   - Users cannot discover or access credentials owned by other users
   - Credential listing is filtered by ownership
   - Direct ID access is permission-checked

### Credential Lifecycle

1. **Creation:**
   - User must select a credential type before input
   - All required fields must be populated
   - Optional notes field for user documentation
   - Unique name per user recommended (not enforced)

2. **Storage:**
   - Credentials stored indefinitely until explicitly deleted
   - No automatic expiration (except OAuth token expiration tracking)
   - Metadata always accessible to owner
   - Sensitive data decrypted only on explicit request

3. **Update:**
   - Full credential update (not partial field updates)
   - Re-encryption performed on every update
   - Name and notes can be updated independently
   - Type cannot be changed after creation

4. **Deletion:**
   - Permanent deletion with no recovery
   - Cascade deletion when user account is deleted
   - Confirmation required before deletion
   - No soft delete or archive functionality

### Security Policies

1. **Encryption at Rest:**
   - All sensitive fields encrypted before database storage
   - Encryption performed application-side, not database-side
   - Different credential instances encrypted independently

2. **Decryption Access:**
   - Metadata (name, type, notes) readable without decryption
   - Sensitive data decrypted only via explicit "with-data" endpoint
   - Decryption logged for audit purposes
   - No client-side decryption

3. **Data Separation:**
   - Credential type stored in plain text for filtering
   - Encrypted blob contains all sensitive fields as JSON
   - No mixing of encrypted and plain-text sensitive data

4. **Key Management:**
   - Single encryption key for all credentials (application-level)
   - Key derived from secure random seed
   - Key rotation not currently supported (future consideration)

## User Workflows

### Creating a Credential

1. User navigates to Credentials page
2. Clicks "Add Credential" button
3. Provides credential name and optional notes
4. Selects credential type from dropdown
5. Form dynamically renders type-specific fields
6. User fills all required fields
7. Clicks "Save"
8. System validates, encrypts, and stores credential
9. User sees success confirmation
10. Credential appears in credentials list

### Editing a Credential

1. User locates credential in list
2. Clicks action menu (three dots) and selects "Edit"
3. System fetches and decrypts credential data
4. Form pre-populates with current values
5. User modifies desired fields
6. Clicks "Save"
7. System re-encrypts and updates credential
8. User sees success confirmation
9. Updated credential reflected in list

### Using a Credential

1. Workflow agent or script requests credential by ID
2. System verifies requestor authorization (user context)
3. Credential is decrypted on-demand
4. Decrypted data provided to authorized consumer
5. Consumer uses credential for external service integration
6. Decryption event logged for security audit

### Deleting a Credential

1. User locates credential in list
2. Clicks action menu and selects "Delete"
3. System displays confirmation dialog with warning
4. User confirms deletion
5. Credential permanently removed from system
6. User sees success confirmation
7. Credential removed from list

## Data Model (Conceptual)

### Credential Entity

**Attributes:**
- **ID:** Unique identifier (UUID)
- **Owner ID:** Foreign key to user account
- **Name:** User-defined display name (1-255 characters)
- **Type:** Credential type enum (email_imap | odoo | gmail_oauth)
- **Encrypted Data:** Encrypted JSON blob containing sensitive fields
- **Notes:** Optional user notes (plain text, unlimited length)
- **Created At:** Timestamp (auto-generated)
- **Updated At:** Timestamp (auto-updated)

**Relationships:**
- Belongs to: User (owner)
- Referenced by: Workflows, Agents (future)

**Constraints:**
- Name is required
- Type is required and immutable after creation
- Encrypted data is required
- Owner ID must reference valid user
- Deletion cascades when owner is deleted

### Credential Type Schemas

Each type defines its own schema validated during creation/update:

**Email IMAP:**
```
{
  "host": string (required),
  "port": integer (required, 1-65535),
  "login": string (required),
  "password": string (required),
  "is_ssl": boolean (default: true)
}
```

**Odoo:**
```
{
  "url": string (required, valid URL),
  "database_name": string (required),
  "login": string (required),
  "api_token": string (required)
}
```

**Gmail OAuth:**
```
{
  "access_token": string (required),
  "refresh_token": string (optional),
  "token_type": string (default: "Bearer"),
  "expires_at": integer (optional, Unix timestamp),
  "scope": string (optional)
}
```

## API Surface

### Credential Operations

**List Credentials** (without sensitive data):
- Returns: credential metadata for all user-owned credentials
- Filtering: by credential type (optional)
- Pagination: supported via skip/limit
- Access: owner or superuser

**Get Credential** (metadata only):
- Returns: single credential metadata
- Access: owner or superuser

**Get Credential with Data** (includes decrypted fields):
- Returns: credential metadata + decrypted sensitive data
- Access: owner or superuser
- Use: for consumption by workflows/scripts

**Create Credential**:
- Input: name, type, notes, credential_data (type-specific)
- Validation: schema validation per type
- Returns: created credential metadata
- Access: authenticated user

**Update Credential**:
- Input: updated name, notes, and/or credential_data
- Validation: schema validation per type
- Returns: updated credential metadata
- Access: owner or superuser

**Delete Credential**:
- Input: credential ID
- Returns: success confirmation
- Access: owner or superuser
- Effect: permanent deletion

## Security Architecture

### Encryption Strategy

**Algorithm:** AES-256 via Fernet (symmetric encryption)

**Key Derivation:**
- Base key: stored in application configuration (environment variable)
- Derived key: PBKDF2-HMAC-SHA256 with 100,000 iterations
- Salt: static application-level salt (deterministic)

**Encryption Scope:**
- All fields in `credential_data` encrypted as single JSON blob
- Metadata (name, type, notes) stored in plain text
- Encryption performed application-side before database write

**Decryption:**
- On-demand only (not automatic on fetch)
- Separate API endpoint for decrypted access
- Decryption occurs in-memory, never persisted

### Threat Model

**Protected Against:**
- ✅ Database breach (data encrypted at rest)
- ✅ SQL injection (parameterized queries)
- ✅ Unauthorized access (ownership + permission checks)
- ✅ Cross-user access (ownership filtering)
- ✅ Accidental exposure (sensitive data requires explicit endpoint)

**Not Protected Against:**
- ❌ Application-level compromise (encryption key accessible to app)
- ❌ Memory dumps during decryption (credentials in-memory briefly)
- ❌ Key exposure via configuration leak
- ❌ Insider threat with database + config access

**Mitigation Strategies:**
- Regular key rotation (future enhancement)
- Application security hardening (principle of least privilege)
- Audit logging for decryption events (future enhancement)
- Secrets management service for key storage (future enhancement)

## Future Considerations

### Planned Enhancements

1. **Credential Sharing:**
   - Allow users to grant read access to specific credentials
   - Team-level credential pools
   - Role-based access control (RBAC)

2. **Credential Versioning:**
   - Track credential update history
   - Ability to rollback to previous version
   - Change audit trail

3. **OAuth Token Management:**
   - Automatic token refresh using refresh tokens
   - Token expiration notifications
   - OAuth flow initiation from UI

4. **Enhanced Security:**
   - User-specific encryption keys (separate key per user)
   - Encryption key rotation mechanism
   - Hardware security module (HSM) integration
   - Audit logging for all credential access

5. **Credential Templates:**
   - Pre-defined templates for common services
   - Guided credential creation wizards
   - Integration testing (validate credentials work)

6. **Usage Tracking:**
   - Track which workflows use which credentials
   - Last used timestamp
   - Usage analytics and dashboards

7. **Additional Credential Types:**
   - AWS IAM credentials
   - Database connection strings
   - SSH keys
   - API keys (generic)
   - LDAP/Active Directory
   - Certificate stores

### Integration Points

**Current:**
- Credentials stored centrally for future use
- UI for full CRUD lifecycle management

**Future:**
- Workflow engine credential injection
- Agent authentication via stored credentials
- Scheduled task credential access
- API webhook authentication

## Success Metrics

**Security Metrics:**
- Zero unauthorized credential access incidents
- 100% encryption coverage for sensitive fields
- Encryption/decryption performance < 100ms per operation

**Usability Metrics:**
- Time to create credential < 2 minutes
- User-reported credential management satisfaction > 4/5
- Zero credential-related workflow failures due to system issues

**Adoption Metrics:**
- Average credentials per active user
- Credential type distribution
- Credential usage frequency in workflows

## Compliance Considerations

**Data Protection:**
- Credentials contain personal data (email addresses, usernames)
- Encryption provides security safeguard for compliance (GDPR, CCPA)
- User data deletion must include credential cascade deletion

**Access Control:**
- User consent required for credential creation
- Clear ownership and access patterns
- No third-party access without user authorization

**Audit Requirements:**
- Future: complete audit trail for credential access
- Retention: credential access logs retained per compliance policy
- Reporting: audit reports for security reviews

## Conclusion

The Credentials Management feature provides a secure, user-friendly foundation for storing sensitive authentication data required by workflow automation. The current implementation focuses on security (encryption at rest), usability (type-specific forms), and access control (user ownership). Future enhancements will expand sharing capabilities, improve security posture, and integrate deeply with workflow execution engines.
