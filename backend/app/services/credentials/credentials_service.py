"""
Credentials Service - Business logic for credential operations.
"""
import uuid
import json
import copy
import logging
import re
from sqlmodel import Session, select
from sqlalchemy import func as func_sql
from app.core.security import encrypt_field, decrypt_field
from app.core.ssh_key_utils import (
    calculate_fingerprint,
    detect_key_type,
    generate_ed25519_key_pair,
    generate_rsa_key_pair,
    is_private_key_encrypted,
    validate_key_pair,
)
from app.models import Credential, Agent, AgentEnvironment, AgentCredentialLink, CredentialCreate, CredentialUpdate

logger = logging.getLogger(__name__)

# Legal characters in an SSH `Host` pattern. Matches OpenSSH host pattern syntax
# (alnum, dot, hyphen, underscore, wildcards `*` / `?`, bracket groups). Rejects
# whitespace, newlines, and control chars — a defence-in-depth layer that
# prevents a malicious `host_aliases` value from injecting arbitrary SSH config
# directives into the generated `~/.ssh/config` (e.g., an alias containing a
# newline could add an `IdentityFile /etc/shadow` line).
_SSH_HOST_ALIAS_RE = re.compile(r"^[A-Za-z0-9_.\-*?\[\]]+$")


class CredentialsService:
    """
    Service for managing credentials and syncing them to agent environments.

    Responsibilities:
    - Prepare credentials data for agents (with decryption)
    - Redact sensitive fields for agent prompts
    - Format credentials for different environments
    """

    # Fields to redact in credentials (by credential type) - for README/prompt display
    SENSITIVE_FIELDS = {
        "email_imap": ["password"],
        "email_smtp": ["password"],
        "odoo": ["api_token"],
        "gmail_oauth": ["access_token", "refresh_token"],
        "gmail_oauth_readonly": ["access_token", "refresh_token"],
        "gdrive_oauth": ["access_token", "refresh_token"],
        "gdrive_oauth_readonly": ["access_token", "refresh_token"],
        "gcalendar_oauth": ["access_token", "refresh_token"],
        "gcalendar_oauth_readonly": ["access_token", "refresh_token"],
        "api_token": ["http_header_value"],
        "google_service_account": ["private_key", "private_key_id"],
        # ssh_key: belt-and-suspenders — these fields are already stripped by the
        # AGENT_ENV_ALLOWED_FIELDS whitelist below, but redaction protects us if a
        # future change accidentally leaks them into the README render.
        "ssh_key": ["private_key", "passphrase"],
    }

    # WHITELIST: Fields that ARE allowed to be exposed to agent environment
    # Security: Only explicitly listed fields are transferred. Any field not listed is excluded.
    # This prevents accidental exposure of new sensitive fields (refresh_token, client_secret, etc.)
    AGENT_ENV_ALLOWED_FIELDS = {
        # Non-OAuth credentials: Need all functional fields for agent to use them
        "email_imap": ["host", "port", "login", "password", "is_ssl"],
        "email_smtp": ["host", "port", "username", "password", "from_email", "use_tls", "use_ssl"],
        "odoo": ["url", "database_name", "login", "api_token"],
        "api_token": ["http_header_name", "http_header_value"],
        "google_service_account": ["file_path", "project_id", "client_email"],

        # OAuth credentials: Only expose access token and metadata
        # refresh_token and client_secret are NEVER included (backend handles token refresh)
        "gmail_oauth": [
            "access_token",      # Required for API calls
            "token_type",        # Usually "Bearer"
            "expires_at",        # Token expiration timestamp
            "scope",             # Granted scopes
            "granted_user_email", # User's email (for display/logging)
            "granted_user_name"   # User's name (for display/logging)
        ],
        "gmail_oauth_readonly": [
            "access_token",
            "token_type",
            "expires_at",
            "scope",
            "granted_user_email",
            "granted_user_name"
        ],
        "gdrive_oauth": [
            "access_token",
            "token_type",
            "expires_at",
            "scope",
            "granted_user_email",
            "granted_user_name"
        ],
        "gdrive_oauth_readonly": [
            "access_token",
            "token_type",
            "expires_at",
            "scope",
            "granted_user_email",
            "granted_user_name"
        ],
        "gcalendar_oauth": [
            "access_token",
            "token_type",
            "expires_at",
            "scope",
            "granted_user_email",
            "granted_user_name"
        ],
        "gcalendar_oauth_readonly": [
            "access_token",
            "token_type",
            "expires_at",
            "scope",
            "granted_user_email",
            "granted_user_name"
        ],

        # ssh_key: Only public metadata reaches credentials.json. The private key
        # and passphrase travel on the sibling `ssh_keys` bundle (written directly
        # into ~/.ssh/ inside the container) and MUST NEVER be whitelisted here.
        "ssh_key": ["public_key", "fingerprint", "key_type", "host_aliases"],
    }

    @staticmethod
    def decrypt_credential_data(session: Session, credential: Credential) -> dict:
        """Decrypt and return the credential's stored data as a dict."""
        decrypted_json = decrypt_field(credential.encrypted_data)
        return json.loads(decrypted_json)

    @staticmethod
    def get_agent_credentials_with_data(
        session: Session,
        agent_id: uuid.UUID
    ) -> list[dict]:
        """
        Get all credentials for an agent with decrypted data.

        Args:
            session: Database session
            agent_id: Agent ID

        Returns:
            List of credential dictionaries with decrypted data:
            [
                {
                    "id": "uuid",
                    "name": "Gmail Account",
                    "type": "gmail_oauth",
                    "notes": "Personal email",
                    "credential_data": {...}  # Decrypted
                },
                ...
            ]
        """
        # Get credentials for agent
        credentials = CredentialsService.get_agent_credentials(session=session, agent_id=agent_id)

        result = []
        for cred in credentials:
            # Decrypt credential data
            credential_data = CredentialsService.decrypt_credential_data(session=session, credential=cred)

            # Process API Token credentials to generate HTTP header fields
            if cred.type.value == "api_token":
                credential_data = CredentialsService._process_api_token_credential(credential_data)

            result.append({
                "id": str(cred.id),
                "name": cred.name,
                "type": cred.type.value,
                "notes": cred.notes,
                "credential_data": credential_data
            })

        return result

    @staticmethod
    def _process_api_token_credential(credential_data: dict) -> dict:
        """
        Process API Token credential to generate ready-to-use HTTP header fields.

        Converts:
            {
                "api_token_type": "bearer" | "custom",
                "api_token_template": "Authorization: Bearer {TOKEN}",  # if custom
                "api_token": "secret_token"
            }

        To:
            {
                "http_header_name": "Authorization",
                "http_header_value": "Bearer secret_token"
            }

        Args:
            credential_data: Raw credential data with template fields

        Returns:
            Processed credential data with http_header_name and http_header_value
        """
        api_token_type = credential_data.get("api_token_type", "bearer")
        api_token = credential_data.get("api_token", "")

        if api_token_type == "bearer":
            # Default bearer token
            return {
                "http_header_name": "Authorization",
                "http_header_value": f"Bearer {api_token}"
            }
        else:
            # Custom template
            template = credential_data.get("api_token_template", "Authorization: Bearer {TOKEN}")

            # Replace {TOKEN} placeholder with actual token
            header_string = template.replace("{TOKEN}", api_token)

            # Parse header name and value
            if ":" in header_string:
                header_name, header_value = header_string.split(":", 1)
                return {
                    "http_header_name": header_name.strip(),
                    "http_header_value": header_value.strip()
                }
            else:
                # Fallback: treat the whole string as header value with Authorization header
                return {
                    "http_header_name": "Authorization",
                    "http_header_value": header_string
                }

    @staticmethod
    def validate_service_account_json(credential_data: dict) -> None:
        """
        Validate that credential_data is a valid Google Service Account JSON key.

        Args:
            credential_data: Dictionary from the parsed JSON key file

        Raises:
            ValueError: If validation fails
        """
        if not credential_data:
            raise ValueError("Service account JSON data is required")

        sa_type = credential_data.get("type")
        if sa_type != "service_account":
            raise ValueError(
                f"Invalid service account JSON: 'type' field must be 'service_account', "
                f"got '{sa_type}'"
            )

        required_fields = ["project_id", "private_key_id", "private_key", "client_email"]
        missing = [f for f in required_fields if not credential_data.get(f)]
        if missing:
            raise ValueError(
                f"Invalid service account JSON: missing required fields: {', '.join(missing)}"
            )

    # ------------------------------------------------------------------ #
    # SSH Key credential helpers                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def process_ssh_key_credential_input(
        raw_data: dict,
        credential_name: str | None = None,
    ) -> dict:
        """
        Process an ssh_key credential's create/update payload into the normalized
        blob stored (Fernet-encrypted) in `Credential.encrypted_data`.

        Accepts two modes:
          - `mode=generate`: server generates the key pair. `key_type` defaults to
            `rsa` (4096-bit). `ed25519` also supported.
          - `mode=import`: client supplies `public_key` + `private_key`, plus
            optional `passphrase` and `host_aliases`.

        Args:
            raw_data: The `credential_data` dict from the API request.
            credential_name: Optional label used as the public key comment when
                generating.

        Returns:
            Normalized blob: {
                "public_key": str,
                "private_key": str,
                "passphrase": str | None,
                "fingerprint": str,
                "key_type": str,
                "host_aliases": list[str] | None,
            }

        Raises:
            ValueError: On malformed input. Message starts with the offending field
                name so the route can surface inline errors.
        """
        if not raw_data or not isinstance(raw_data, dict):
            raise ValueError("credential_data is required for ssh_key credentials")

        mode = raw_data.get("mode")
        if mode not in ("generate", "import"):
            raise ValueError(
                "credential_data.mode must be 'generate' or 'import' for ssh_key credentials"
            )

        host_aliases = raw_data.get("host_aliases") or None
        if host_aliases is not None:
            if not isinstance(host_aliases, list) or not all(
                isinstance(a, str) and a.strip() for a in host_aliases
            ):
                raise ValueError(
                    "credential_data.host_aliases must be a list of non-empty strings"
                )
            # Normalise — trim, validate against SSH-host-pattern regex (defence
            # in depth against ssh_config injection), and deduplicate while
            # preserving order.
            seen = set()
            deduped = []
            for alias in host_aliases:
                trimmed = alias.strip()
                if not trimmed:
                    continue
                if not _SSH_HOST_ALIAS_RE.match(trimmed):
                    raise ValueError(
                        f"Invalid host alias {trimmed!r}: only alphanumerics, "
                        "'.', '-', '_', and wildcards '*?[]' are allowed."
                    )
                if trimmed not in seen:
                    seen.add(trimmed)
                    deduped.append(trimmed)
            host_aliases = deduped or None

        if mode == "generate":
            return CredentialsService._generate_ssh_key_pair(
                key_type=(raw_data.get("key_type") or "rsa").lower(),
                name=credential_name or "cinna-agent-key",
                host_aliases=host_aliases,
            )

        return CredentialsService._import_ssh_key_pair(
            public_key=raw_data.get("public_key", ""),
            private_key=raw_data.get("private_key", ""),
            passphrase=raw_data.get("passphrase"),
            host_aliases=host_aliases,
        )

    @staticmethod
    def _generate_ssh_key_pair(
        key_type: str,
        name: str,
        host_aliases: list[str] | None,
    ) -> dict:
        """Generate a fresh SSH key pair and return the normalized credential blob."""
        if key_type == "ed25519":
            public_key, private_key = generate_ed25519_key_pair(name)
        elif key_type == "rsa":
            public_key, private_key = generate_rsa_key_pair(name)
        else:
            raise ValueError(
                "credential_data.key_type must be 'rsa' or 'ed25519' for generate mode"
            )

        fingerprint = calculate_fingerprint(public_key)
        return {
            "public_key": public_key,
            "private_key": private_key,
            "passphrase": None,
            "fingerprint": fingerprint,
            "key_type": detect_key_type(public_key),
            "host_aliases": host_aliases,
        }

    @staticmethod
    def _import_ssh_key_pair(
        public_key: str,
        private_key: str,
        passphrase: str | None,
        host_aliases: list[str] | None,
    ) -> dict:
        """Validate and normalise an imported SSH key pair."""
        public_key = (public_key or "").strip()
        private_key = (private_key or "").strip()

        if not public_key:
            raise ValueError("public_key is required when mode='import'")
        if not private_key:
            raise ValueError("private_key is required when mode='import'")

        # Structural validation (prefix + PEM markers). Raises ValueError with a
        # field-specific message that the route surfaces as 422 detail.
        validate_key_pair(public_key, private_key)

        # MVP: reject passphrase-encrypted private keys. Plan's Error Handling
        # table: "Encrypted private keys are not yet supported — please export
        # without passphrase or generate a new key."
        if passphrase or is_private_key_encrypted(private_key):
            raise ValueError(
                "Encrypted private keys are not yet supported — please export "
                "without passphrase or generate a new key."
            )

        fingerprint = calculate_fingerprint(public_key)
        return {
            "public_key": public_key,
            "private_key": private_key,
            "passphrase": None,  # MVP: never persist a passphrase
            "fingerprint": fingerprint,
            "key_type": detect_key_type(public_key),
            "host_aliases": host_aliases,
        }

    @staticmethod
    def prepare_ssh_key_update_data(
        session: Session,
        credential: Credential,
        raw_data: dict,
        credential_name: str | None,
    ) -> dict:
        """
        Normalise `credential_data` for an ssh_key credential update.

        Two update paths are supported:
          1. Key rotation / re-import — `mode` is present in `raw_data`; delegates
             to `process_ssh_key_credential_input` (same path used on create).
          2. Metadata-only update — `mode` absent; the only editable field is
             `host_aliases`. Other keys (e.g., `public_key`, `private_key`) are
             rejected with 422 so callers can't sneak in key material without
             the rotation flow.

        The existing blob is decrypted and merged with the allowed metadata
        updates so immutable fields (public_key, private_key, fingerprint,
        key_type) are preserved verbatim.

        Args:
            session: Database session (used only for decryption of the existing
                blob).
            credential: The Credential row being updated.
            raw_data: `credential_data` dict from the API request.
            credential_name: Name on the CredentialUpdate, or falls back to the
                existing credential's name when generating a fresh key.

        Returns:
            A dict ready to be Fernet-encrypted and stored.

        Raises:
            ValueError: On malformed input or disallowed fields. The route maps
                these to HTTP 422.
        """
        if "mode" in raw_data:
            return CredentialsService.process_ssh_key_credential_input(
                raw_data,
                credential_name=credential_name or credential.name,
            )

        allowed = {"host_aliases"}
        unknown = set(raw_data.keys()) - allowed
        if unknown:
            raise ValueError(
                "credential_data may only update host_aliases for ssh_key "
                f"credentials (got: {sorted(unknown)}). To rotate or re-import "
                "the key, include `mode='generate'` or `mode='import'`."
            )

        # Reuse the input processor's host_aliases validation + normalisation by
        # routing through a minimal stub. We cannot call
        # process_ssh_key_credential_input directly (it requires `mode`), so
        # inline the normalisation here — kept in lockstep with the create path.
        aliases = raw_data.get("host_aliases")
        normalised_aliases: list[str] | None = None
        if aliases is not None:
            if not isinstance(aliases, list) or not all(
                isinstance(a, str) and a.strip() for a in aliases
            ):
                raise ValueError(
                    "host_aliases must be a list of non-empty strings"
                )
            seen: set[str] = set()
            for alias in aliases:
                trimmed = alias.strip()
                if not trimmed:
                    continue
                if not _SSH_HOST_ALIAS_RE.match(trimmed):
                    raise ValueError(
                        f"Invalid host alias {trimmed!r}: only alphanumerics, "
                        "'.', '-', '_', and wildcards '*?[]' are allowed."
                    )
                if trimmed not in seen:
                    seen.add(trimmed)
                    normalised_aliases = normalised_aliases or []
                    normalised_aliases.append(trimmed)

        existing = CredentialsService.decrypt_credential_data(
            session=session, credential=credential
        )
        if "host_aliases" in raw_data:
            existing["host_aliases"] = normalised_aliases
        return existing

    @staticmethod
    def _process_ssh_key_for_env(credential_data: dict) -> dict:
        """
        Convert the full SSH key blob into the metadata that is safe to expose
        inside `credentials.json`.

        NEVER includes `private_key` or `passphrase` — those live only on the
        sibling `ssh_keys` bundle written directly into `~/.ssh/` by the agent env.
        """
        return {
            "public_key": credential_data.get("public_key", ""),
            "fingerprint": credential_data.get("fingerprint", ""),
            "key_type": credential_data.get("key_type", ""),
            "host_aliases": credential_data.get("host_aliases") or ["*"],
        }

    @staticmethod
    def _process_service_account_credential(credential_data: dict, credential_id: str) -> dict:
        """
        Process Google Service Account credential for agent environment.

        Converts the full SA JSON into a reference dict with file_path and metadata.
        The actual JSON file is written separately by the agent environment.

        Args:
            credential_data: Full service account JSON data
            credential_id: Credential UUID string

        Returns:
            Dict with file_path, project_id, and client_email
        """
        return {
            "file_path": f"credentials/{credential_id}.json",
            "project_id": credential_data.get("project_id", ""),
            "client_email": credential_data.get("client_email", ""),
        }

    @staticmethod
    def filter_credential_data_for_agent_env(credential_type: str, credential_data: dict) -> dict:
        """
        Filter credential data using WHITELIST approach before exposing to agent environment.

        Security: Only explicitly allowed fields are included. Any field not in the whitelist
        is excluded, preventing accidental exposure of sensitive data (refresh tokens,
        client secrets, etc.).

        Whitelist rationale:
        - OAuth credentials: Only access_token + metadata (no refresh_token or client_secret)
        - Non-OAuth credentials: Only functional fields needed by agent
        - Unknown credential types: Empty dict (fail-safe)

        Args:
            credential_type: Type of credential
            credential_data: Original credential data

        Returns:
            New dict containing ONLY whitelisted fields that exist in original data
        """
        # Get allowed fields for this credential type
        allowed_fields = CredentialsService.AGENT_ENV_ALLOWED_FIELDS.get(credential_type, [])

        if not allowed_fields:
            logger.warning(
                f"No allowed fields defined for credential type '{credential_type}'. "
                f"Credential will be empty in agent environment. "
                f"Add this type to AGENT_ENV_ALLOWED_FIELDS if it should be accessible."
            )
            return {}

        # Build new dict with ONLY whitelisted fields
        filtered = {}
        for field in allowed_fields:
            if field in credential_data:
                filtered[field] = credential_data[field]
                logger.debug(f"Including '{field}' in {credential_type} for agent env")
            else:
                logger.debug(f"Field '{field}' not found in {credential_type} data (expected for whitelist)")

        # Log any fields that were excluded
        excluded_fields = set(credential_data.keys()) - set(filtered.keys())
        if excluded_fields:
            logger.info(
                f"Excluded {len(excluded_fields)} field(s) from {credential_type} "
                f"before agent env sync: {sorted(excluded_fields)}"
            )

        return filtered

    @staticmethod
    def redact_credential_data(credential_type: str, credential_data: dict) -> dict:
        """
        Redact sensitive fields from credential data for use in agent prompts.

        Only redacts fields that have actual values. Empty/null fields are safe to show
        since they indicate missing data that the user needs to configure.

        Args:
            credential_type: Type of credential (email_imap, odoo, gmail_oauth)
            credential_data: Original credential data

        Returns:
            Redacted copy of credential data with sensitive fields replaced by "***REDACTED***"
            (only if they have actual values)
        """
        # Create a deep copy to avoid modifying original
        redacted = copy.deepcopy(credential_data)

        # Get sensitive fields for this credential type
        sensitive_fields = CredentialsService.SENSITIVE_FIELDS.get(credential_type, [])

        # Redact each sensitive field ONLY if it has a non-empty value
        for field in sensitive_fields:
            if field in redacted and redacted[field]:
                # Only redact if the field has an actual value (not empty string, not None)
                redacted[field] = "***REDACTED***"

        return redacted

    @staticmethod
    def generate_credentials_readme(credentials: list[dict]) -> str:
        """
        Generate a README.md content for credentials with redacted sensitive data.

        This README will be included in the building agent prompt so the agent
        knows what credentials are available and how to use them, but doesn't
        see the actual sensitive values.

        Args:
            credentials: List of credentials with decrypted data

        Returns:
            Markdown content for credentials/README.md
        """
        if not credentials:
            return """# Credentials

No credentials are currently shared with this agent.

If you need credentials for integrations (email, APIs, databases), ask the user to share them with this agent.
"""

        # Build markdown content
        lines = [
            "# Credentials",
            "",
            "This agent has access to the following credentials for integrations and automation.",
            "",
            "## Important Security Rules",
            "",
            "1. **NEVER read credentials directly** from `credentials/credentials.json`",
            "2. **ALWAYS access credentials programmatically** in your scripts",
            "3. **NEVER log or output credential values** in messages or files",
            "4. **Use credentials ONLY** for their intended purpose",
            "",
            "## How to Access Credentials",
            "",
            "Read the credentials file in your Python scripts:",
            "",
            "```python",
            "import json",
            "from pathlib import Path",
            "",
            "# Load credentials",
            "credentials_file = Path('credentials/credentials.json')",
            "with open(credentials_file, 'r') as f:",
            "    all_credentials = json.load(f)",
            "",
            "# Find specific credential by ID (recommended, IDs never change)",
            "credential_id = '6a32aeb0-3a26-43eb-ab2b-d9df720be807'  # Use actual ID from list below",
            "for cred in all_credentials:",
            "    if cred['id'] == credential_id:",
            "        config = cred['credential_data']",
            "        # Use config fields based on credential type",
            "        break",
            "",
            "# Or find by type (if you only have one credential of this type)",
            "for cred in all_credentials:",
            "    if cred['type'] == 'email_imap':",
            "        config = cred['credential_data']",
            "        # Use config['host'], config['login'], etc.",
            "        break",
            "```",
            "",
            "## Available Credentials",
            "",
        ]

        # Build list of credentials with redacted data for display
        credentials_for_display = []
        for cred in credentials:
            # Redact sensitive data in credential_data
            redacted_credential_data = CredentialsService.redact_credential_data(
                cred["type"],
                cred["credential_data"]
            )

            # Build credential object matching JSON structure
            credentials_for_display.append({
                "id": cred["id"],
                "name": cred["name"],
                "type": cred["type"],
                "notes": cred["notes"],
                "credential_data": redacted_credential_data
            })

        # Show the full structure as JSON array (matching credentials.json format)
        lines.append("The credentials file (`credentials/credentials.json`) contains:")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(credentials_for_display, indent=2))
        lines.append("```")
        lines.append("")
        lines.append("**Note**: Sensitive fields (passwords, tokens) are shown as `***REDACTED***` if they contain values.")
        lines.append("")
        lines.append("---")
        lines.append("")

        # Add usage examples for each credential type that has data
        has_examples = False
        for cred in credentials:
            cred_type = cred["type"]
            credential_data = cred["credential_data"]

            # Skip usage examples for empty credentials
            if not credential_data or credential_data == {}:
                continue

            if not has_examples:
                lines.append("## Usage Examples")
                lines.append("")
                has_examples = True

            # Add type-specific usage hints (only for credentials with data)
            cred_name = cred["name"]
            cred_id = cred["id"]
            if cred_type == "email_imap":
                lines.append(f"### IMAP Credential: {cred_name}")
                lines.append(f"**ID**: `{cred_id}`")
                lines.append("")
                lines.append("```python")
                lines.append("import json")
                lines.append("import imaplib")
                lines.append("")
                lines.append("# Load credentials")
                lines.append("with open('credentials/credentials.json', 'r') as f:")
                lines.append("    all_credentials = json.load(f)")
                lines.append("")
                lines.append(f"# Find credential by ID (recommended)")
                lines.append(f"credential_id = '{cred_id}'")
                lines.append("for cred in all_credentials:")
                lines.append("    if cred['id'] == credential_id:")
                lines.append("        config = cred['credential_data']")
                lines.append("        # Connect to IMAP server")
                lines.append("        if config.get('is_ssl', True):")
                lines.append("            mail = imaplib.IMAP4_SSL(config['host'], config['port'])")
                lines.append("        else:")
                lines.append("            mail = imaplib.IMAP4(config['host'], config['port'])")
                lines.append("        mail.login(config['login'], config['password'])")
                lines.append("        # ... use mail connection")
                lines.append("        mail.logout()")
                lines.append("        break")
                lines.append("```")
                lines.append("")
            elif cred_type == "odoo":
                lines.append(f"### Odoo Credential: {cred_name}")
                lines.append(f"**ID**: `{cred_id}`")
                lines.append("")
                lines.append("```python")
                lines.append("import json")
                lines.append("import xmlrpc.client")
                lines.append("")
                lines.append("# Load credentials")
                lines.append("with open('credentials/credentials.json', 'r') as f:")
                lines.append("    all_credentials = json.load(f)")
                lines.append("")
                lines.append(f"# Find credential by ID (recommended)")
                lines.append(f"credential_id = '{cred_id}'")
                lines.append("for cred in all_credentials:")
                lines.append("    if cred['id'] == credential_id:")
                lines.append("        config = cred['credential_data']")
                lines.append("        # Connect to Odoo")
                lines.append("        common = xmlrpc.client.ServerProxy(f\"{config['url']}/xmlrpc/2/common\")")
                lines.append("        uid = common.authenticate(")
                lines.append("            config['database_name'],")
                lines.append("            config['login'],")
                lines.append("            config['api_token'],")
                lines.append("            {}")
                lines.append("        )")
                lines.append("        # ... use Odoo API")
                lines.append("        break")
                lines.append("```")
                lines.append("")
            elif cred_type in ["gmail_oauth", "gmail_oauth_readonly"]:
                readonly_suffix = " (Read-Only)" if "readonly" in cred_type else ""
                lines.append(f"### Gmail OAuth Credential{readonly_suffix}: {cred_name}")
                lines.append(f"**ID**: `{cred_id}`")
                lines.append("")
                lines.append("**Note**: Tokens are automatically refreshed by the platform. Your script will always get fresh credentials.")
                lines.append("")
                lines.append("```python")
                lines.append("import json")
                lines.append("from google.oauth2.credentials import Credentials")
                lines.append("from googleapiclient.discovery import build")
                lines.append("")
                lines.append("# Load credentials")
                lines.append("with open('credentials/credentials.json', 'r') as f:")
                lines.append("    all_credentials = json.load(f)")
                lines.append("")
                lines.append(f"# Find credential by ID (recommended)")
                lines.append(f"credential_id = '{cred_id}'")
                lines.append("for cred in all_credentials:")
                lines.append("    if cred['id'] == credential_id:")
                lines.append("        # Use Gmail API")
                lines.append("        creds = Credentials.from_authorized_user_info(cred['credential_data'])")
                lines.append("        service = build('gmail', 'v1', credentials=creds)")
                lines.append("")
                lines.append("        # Example: List messages")
                lines.append("        results = service.users().messages().list(userId='me', maxResults=10).execute()")
                lines.append("        messages = results.get('messages', [])")
                lines.append("")
                if "readonly" not in cred_type:
                    lines.append("        # Example: Send an email")
                    lines.append("        from email.mime.text import MIMEText")
                    lines.append("        import base64")
                    lines.append("        message = MIMEText('Email body')")
                    lines.append("        message['to'] = 'recipient@example.com'")
                    lines.append("        message['subject'] = 'Subject'")
                    lines.append("        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()")
                    lines.append("        service.users().messages().send(")
                    lines.append("            userId='me', body={'raw': raw}")
                    lines.append("        ).execute()")
                    lines.append("")
                lines.append("        break")
                lines.append("```")
                lines.append("")
            elif cred_type in ["gdrive_oauth", "gdrive_oauth_readonly"]:
                readonly_suffix = " (Read-Only)" if "readonly" in cred_type else ""
                lines.append(f"### Google Drive OAuth Credential{readonly_suffix}: {cred_name}")
                lines.append(f"**ID**: `{cred_id}`")
                lines.append("")
                lines.append("**Note**: Tokens are automatically refreshed by the platform. Your script will always get fresh credentials.")
                lines.append("")
                lines.append("```python")
                lines.append("import json")
                lines.append("from google.oauth2.credentials import Credentials")
                lines.append("from googleapiclient.discovery import build")
                lines.append("from googleapiclient.http import MediaFileUpload")
                lines.append("")
                lines.append("# Load credentials")
                lines.append("with open('credentials/credentials.json', 'r') as f:")
                lines.append("    all_credentials = json.load(f)")
                lines.append("")
                lines.append(f"# Find credential by ID (recommended)")
                lines.append(f"credential_id = '{cred_id}'")
                lines.append("for cred in all_credentials:")
                lines.append("    if cred['id'] == credential_id:")
                lines.append("        # Use Google Drive API")
                lines.append("        creds = Credentials.from_authorized_user_info(cred['credential_data'])")
                lines.append("        service = build('drive', 'v3', credentials=creds)")
                lines.append("")
                lines.append("        # Example: List files")
                lines.append("        results = service.files().list(")
                lines.append("            pageSize=10,")
                lines.append("            fields='files(id, name, mimeType)'")
                lines.append("        ).execute()")
                lines.append("        files = results.get('files', [])")
                lines.append("")
                lines.append("        # Example: Download a file")
                lines.append("        file_id = 'file_id_here'")
                lines.append("        request = service.files().get_media(fileId=file_id)")
                lines.append("        with open('downloaded_file.txt', 'wb') as f:")
                lines.append("            f.write(request.execute())")
                lines.append("")
                if "readonly" not in cred_type:
                    lines.append("        # Example: Upload a file")
                    lines.append("        file_metadata = {'name': 'uploaded_file.txt'}")
                    lines.append("        media = MediaFileUpload('local_file.txt', mimetype='text/plain')")
                    lines.append("        file = service.files().create(")
                    lines.append("            body=file_metadata,")
                    lines.append("            media_body=media,")
                    lines.append("            fields='id'")
                    lines.append("        ).execute()")
                    lines.append("")
                lines.append("        break")
                lines.append("```")
                lines.append("")
            elif cred_type in ["gcalendar_oauth", "gcalendar_oauth_readonly"]:
                readonly_suffix = " (Read-Only)" if "readonly" in cred_type else ""
                lines.append(f"### Google Calendar OAuth Credential{readonly_suffix}: {cred_name}")
                lines.append(f"**ID**: `{cred_id}`")
                lines.append("")
                lines.append("**Note**: Tokens are automatically refreshed by the platform. Your script will always get fresh credentials.")
                lines.append("")
                lines.append("```python")
                lines.append("import json")
                lines.append("from datetime import datetime, timedelta")
                lines.append("from google.oauth2.credentials import Credentials")
                lines.append("from googleapiclient.discovery import build")
                lines.append("")
                lines.append("# Load credentials")
                lines.append("with open('credentials/credentials.json', 'r') as f:")
                lines.append("    all_credentials = json.load(f)")
                lines.append("")
                lines.append(f"# Find credential by ID (recommended)")
                lines.append(f"credential_id = '{cred_id}'")
                lines.append("for cred in all_credentials:")
                lines.append("    if cred['id'] == credential_id:")
                lines.append("        # Use Google Calendar API")
                lines.append("        creds = Credentials.from_authorized_user_info(cred['credential_data'])")
                lines.append("        service = build('calendar', 'v3', credentials=creds)")
                lines.append("")
                lines.append("        # Example: List upcoming events")
                lines.append("        now = datetime.utcnow().isoformat() + 'Z'")
                lines.append("        events_result = service.events().list(")
                lines.append("            calendarId='primary',")
                lines.append("            timeMin=now,")
                lines.append("            maxResults=10,")
                lines.append("            singleEvents=True,")
                lines.append("            orderBy='startTime'")
                lines.append("        ).execute()")
                lines.append("        events = events_result.get('items', [])")
                lines.append("")
                if "readonly" not in cred_type:
                    lines.append("        # Example: Create an event")
                    lines.append("        event = {")
                    lines.append("            'summary': 'Meeting',")
                    lines.append("            'start': {")
                    lines.append("                'dateTime': (datetime.now() + timedelta(days=1)).isoformat(),")
                    lines.append("                'timeZone': 'UTC',")
                    lines.append("            },")
                    lines.append("            'end': {")
                    lines.append("                'dateTime': (datetime.now() + timedelta(days=1, hours=1)).isoformat(),")
                    lines.append("                'timeZone': 'UTC',")
                    lines.append("            },")
                    lines.append("        }")
                    lines.append("        created_event = service.events().insert(")
                    lines.append("            calendarId='primary', body=event")
                    lines.append("        ).execute()")
                    lines.append("")
                lines.append("        break")
                lines.append("```")
                lines.append("")
            elif cred_type == "api_token":
                lines.append(f"### API Token Credential: {cred_name}")
                lines.append(f"**ID**: `{cred_id}`")
                lines.append("")
                lines.append("```python")
                lines.append("import json")
                lines.append("import requests")
                lines.append("")
                lines.append("# Load credentials")
                lines.append("with open('credentials/credentials.json', 'r') as f:")
                lines.append("    all_credentials = json.load(f)")
                lines.append("")
                lines.append(f"# Find credential by ID (recommended)")
                lines.append(f"credential_id = '{cred_id}'")
                lines.append("for cred in all_credentials:")
                lines.append("    if cred['id'] == credential_id:")
                lines.append("        config = cred['credential_data']")
                lines.append("        # Use pre-processed HTTP header")
                lines.append("        headers = {")
                lines.append("            config['http_header_name']: config['http_header_value']")
                lines.append("        }")
                lines.append("        ")
                lines.append("        # Make API request")
                lines.append("        response = requests.get('https://api.example.com/endpoint', headers=headers)")
                lines.append("        # ... use response")
                lines.append("        break")
                lines.append("```")
                lines.append("")
            elif cred_type == "ssh_key":
                lines.append(f"### SSH Key Credential: {cred_name}")
                lines.append(f"**ID**: `{cred_id}`")
                lines.append("")
                lines.append(
                    "**Note**: The private key is materialized inside the container at "
                    "`~/.ssh/id_<credential_id>` (0600) and wired into `~/.ssh/config` "
                    "automatically. `git clone git@host:repo` and `ssh host` work "
                    "without further setup. The key body is NOT available to scripts — "
                    "only public metadata (`public_key`, `fingerprint`, `key_type`, "
                    "`host_aliases`) appears in credentials.json."
                )
                lines.append("")
                lines.append("```bash")
                lines.append("# Example: clone a private repo using the key")
                lines.append("git clone git@github.com:your-org/your-repo.git ./files/repositories/your-repo")
                lines.append("")
                lines.append("# Example: inspect which key is loaded")
                lines.append("ls -la ~/.ssh/")
                lines.append("```")
                lines.append("")
            elif cred_type == "google_service_account":
                lines.append(f"### Google Service Account Credential: {cred_name}")
                lines.append(f"**ID**: `{cred_id}`")
                lines.append("")
                lines.append("**Note**: This credential is stored as a standalone JSON key file. Use the `file_path` field to locate it.")
                lines.append("")
                lines.append("```python")
                lines.append("import json")
                lines.append("from google.oauth2 import service_account")
                lines.append("from googleapiclient.discovery import build")
                lines.append("")
                lines.append("# Load credentials reference")
                lines.append("with open('credentials/credentials.json', 'r') as f:")
                lines.append("    all_credentials = json.load(f)")
                lines.append("")
                lines.append(f"# Find credential by ID (recommended)")
                lines.append(f"credential_id = '{cred_id}'")
                lines.append("for cred in all_credentials:")
                lines.append("    if cred['id'] == credential_id:")
                lines.append("        sa_file_path = cred['credential_data']['file_path']")
                lines.append("        ")
                lines.append("        # Load service account credentials from JSON file")
                lines.append("        creds = service_account.Credentials.from_service_account_file(sa_file_path)")
                lines.append("        ")
                lines.append("        # Example: Use with Google Sheets API")
                lines.append("        sheets_service = build('sheets', 'v4', credentials=creds)")
                lines.append("        ")
                lines.append("        # Example: Use with BigQuery")
                lines.append("        from google.cloud import bigquery")
                lines.append("        bq_client = bigquery.Client(credentials=creds, project=cred['credential_data']['project_id'])")
                lines.append("        break")
                lines.append("```")
                lines.append("")

        lines.append("## Best Practices")
        lines.append("")
        lines.append("1. **Use credential IDs for lookup** - IDs never change, unlike names")
        lines.append("2. **Load credentials at script start** and reuse the connection")
        lines.append("3. **Handle errors gracefully** - credentials might be invalid or expired")
        lines.append("4. **Close connections properly** when done")
        lines.append("5. **Never hardcode credentials** - always read from the credentials file")
        lines.append("")

        return "\n".join(lines)

    @staticmethod
    def prepare_credentials_for_environment(
        session: Session,
        agent_id: uuid.UUID
    ) -> dict:
        """
        Prepare credentials data for syncing to agent environment.

        Security: Filters out sensitive fields (refresh tokens, client secrets) that
        should never be exposed to the agent container. The agent only receives
        the minimum data needed to function (e.g., access tokens but not refresh tokens).

        Returns:
            Dictionary with two keys:
            - "credentials_json": Filtered credentials data (for credentials.json file)
            - "credentials_readme": Redacted README content (for credentials/README.md file)
                                    Based on FILTERED structure to match credentials.json
        """
        # Get credentials with decrypted data
        credentials = CredentialsService.get_agent_credentials_with_data(session, agent_id)

        # Collect service account files before filtering
        service_account_files = []
        for cred in credentials:
            if cred["type"] == "google_service_account" and cred.get("credential_data"):
                service_account_files.append({
                    "credential_id": cred["id"],
                    "json_content": copy.deepcopy(cred["credential_data"])
                })
                # Replace credential_data with processed version for credentials.json
                cred["credential_data"] = CredentialsService._process_service_account_credential(
                    cred["credential_data"], cred["id"]
                )

        # Collect SSH key bundles before whitelisting.
        # `ssh_keys` is the SIBLING payload to `service_account_files`. It carries
        # the private key material out-of-band so it never appears in
        # credentials.json or the prompt README. The env-core writes these keys
        # into `~/.ssh/` (0600) and reconciles orphans on every sync.
        ssh_keys: list[dict] = []
        for cred in credentials:
            if cred["type"] == "ssh_key" and cred.get("credential_data"):
                blob = cred["credential_data"]
                ssh_keys.append({
                    "credential_id": cred["id"],
                    "private_key": blob.get("private_key", ""),
                    "public_key": blob.get("public_key", ""),
                    "passphrase": blob.get("passphrase"),
                    "host_aliases": blob.get("host_aliases") or ["*"],
                })
                # Replace credential_data with the whitelisted metadata shape so
                # the downstream filter sees the safe surface.
                cred["credential_data"] = CredentialsService._process_ssh_key_for_env(blob)
                logger.info(
                    "Prepared SSH key credential %s (fingerprint=%s, key_type=%s) for env sync",
                    cred["id"],
                    blob.get("fingerprint", ""),
                    blob.get("key_type", ""),
                )

        # Filter out sensitive fields that should never be exposed to agent environment
        # (e.g., refresh tokens, client secrets for OAuth credentials)
        filtered_credentials = []
        for cred in credentials:
            filtered_cred = copy.deepcopy(cred)
            filtered_cred["credential_data"] = CredentialsService.filter_credential_data_for_agent_env(
                cred["type"],
                cred["credential_data"]
            )
            filtered_credentials.append(filtered_cred)

        # Generate README with redacted data (for agent prompt context)
        # IMPORTANT: Use filtered_credentials so README matches credentials.json structure
        # This ensures agent sees the same fields in README as in the actual JSON file
        readme_content = CredentialsService.generate_credentials_readme(filtered_credentials)

        return {
            "credentials_json": filtered_credentials,
            "credentials_readme": readme_content,
            "service_account_files": service_account_files,
            "ssh_keys": ssh_keys,
        }

    @staticmethod
    async def sync_credentials_to_agent_environments(
        session: Session,
        agent_id: uuid.UUID
    ):
        """
        Sync credentials to all running environments of an agent.

        This is called when:
        - Credentials are updated
        - Credentials are deleted
        - Credentials are shared/unshared with agent

        Args:
            session: Database session
            agent_id: Agent ID whose environments should be updated
        """
        from app.services.environments.environment_service import EnvironmentService

        # Get all running environments for this agent
        statement = select(AgentEnvironment).where(
            AgentEnvironment.agent_id == agent_id,
            AgentEnvironment.status == "running"
        )
        running_environments = session.exec(statement).all()

        if not running_environments:
            logger.info(f"No running environments for agent {agent_id}, skipping credential sync")
            return

        logger.info(f"Syncing credentials to {len(running_environments)} running environment(s) for agent {agent_id}")

        # Prepare credentials data
        credentials_data = CredentialsService.prepare_credentials_for_environment(
            session=session,
            agent_id=agent_id
        )

        # Get lifecycle manager
        lifecycle_manager = EnvironmentService.get_lifecycle_manager()

        # Sync to each running environment
        for env in running_environments:
            try:
                logger.info(f"Syncing credentials to environment {env.id}")
                adapter = lifecycle_manager.get_adapter(env)
                await adapter.set_credentials(credentials_data)
                logger.info(f"Successfully synced credentials to environment {env.id}")
            except Exception as e:
                logger.error(f"Failed to sync credentials to environment {env.id}: {e}")
                # Continue with other environments even if one fails

    @staticmethod
    def create_credential(
        session: Session,
        credential_in: CredentialCreate,
        owner_id: uuid.UUID
    ) -> Credential:
        """
        Create a new credential.

        Args:
            session: Database session
            credential_in: Credential creation data
            owner_id: Owner user ID

        Returns:
            Created Credential model
        """
        credential_data = credential_in.credential_data if credential_in.credential_data is not None else {}
        encrypted_data = encrypt_field(json.dumps(credential_data))

        template_private_fields = list(credential_in.template_private_fields or [])

        db_credential = Credential(
            name=credential_in.name,
            type=credential_in.type,
            notes=credential_in.notes,
            allow_sharing=credential_in.allow_sharing,
            allow_template_sharing=credential_in.allow_template_sharing,
            template_private_fields=template_private_fields,
            encrypted_data=encrypted_data,
            owner_id=owner_id,
            user_workspace_id=credential_in.user_workspace_id,
        )
        session.add(db_credential)
        session.commit()
        session.refresh(db_credential)
        return db_credential

    @staticmethod
    async def update_credential(
        session: Session,
        credential_id: uuid.UUID,
        credential_in: CredentialUpdate,
        owner_id: uuid.UUID,
        is_superuser: bool = False
    ) -> Credential:
        """
        Update a credential with authorization checks.

        This will trigger automatic sync to all running environments of agents
        that have this credential linked.

        Args:
            session: Database session
            credential_id: Credential ID to update
            credential_in: Update data
            owner_id: User ID making the request
            is_superuser: Whether the user is a superuser

        Returns:
            Updated Credential model

        Raises:
            ValueError: If credential not found or permission denied
        """
        # Verify credential exists and user owns it
        # Credentials are always private - only owner can access
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if credential.owner_id != owner_id:
            raise ValueError("Not enough permissions")

        # Update credential
        update_dict = credential_in.model_dump(exclude_unset=True)
        if "template_private_fields" in update_dict:
            raw_fields = update_dict.pop("template_private_fields") or []
            if not isinstance(raw_fields, list) or not all(
                isinstance(f, str) for f in raw_fields
            ):
                raise ValueError("template_private_fields must be a list of strings")
            credential.template_private_fields = list(raw_fields)
        if "credential_data" in update_dict:
            data_payload = update_dict["credential_data"] or {}
            encrypted_data = encrypt_field(json.dumps(data_payload))
            update_dict.pop("credential_data")
            credential.encrypted_data = encrypted_data
            # Phase 4 install setup gate: a placeholder Credential becomes
            # "real" the moment the saved data passes the per-type
            # completeness check. ``check_credential_completeness`` honours
            # the same required-field map the rest of the platform uses, so
            # template-materialised credentials (where the publisher's
            # non-private fields are pre-filled) only flip out of placeholder
            # mode once the installer supplies the missing private fields.
            if credential.is_placeholder and isinstance(data_payload, dict):
                completeness = CredentialsService.check_credential_completeness(
                    credential_type=credential.type.value,
                    credential_data=data_payload,
                )
                if completeness == "complete":
                    credential.is_placeholder = False
        credential.sqlmodel_update(update_dict)
        session.add(credential)
        session.commit()
        session.refresh(credential)

        # Trigger sync to affected agent environments
        await CredentialsService.event_credential_updated(
            session=session,
            credential_id=credential_id
        )

        return credential

    @staticmethod
    async def delete_credential(
        session: Session,
        credential_id: uuid.UUID,
        owner_id: uuid.UUID,
        is_superuser: bool = False
    ):
        """
        Delete a credential with authorization checks.

        This will trigger automatic sync to all running environments of agents
        that had this credential linked.

        Args:
            session: Database session
            credential_id: Credential ID to delete
            owner_id: User ID making the request
            is_superuser: Whether the user is a superuser

        Raises:
            ValueError: If credential not found or permission denied
        """
        # Verify credential exists and user owns it
        # Credentials are always private - only owner can access
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if credential.owner_id != owner_id:
            raise ValueError("Not enough permissions")

        # Get affected agents BEFORE deletion (links will be cascade deleted)
        affected_agent_ids = CredentialsService.get_affected_agents(session, credential_id)

        # Delete credential
        session.delete(credential)
        session.commit()

        # Trigger sync to affected agent environments
        if affected_agent_ids:
            await CredentialsService.event_credential_deleted(
                session=session,
                credential_id=credential_id,
                agent_ids=affected_agent_ids
            )

    @staticmethod
    def get_credential_with_data(
        session: Session,
        credential_id: uuid.UUID,
        owner_id: uuid.UUID,
        is_superuser: bool = False
    ) -> dict:
        """
        Get credential with decrypted data and authorization checks.

        Args:
            session: Database session
            credential_id: Credential ID
            owner_id: User ID making the request
            is_superuser: Whether the user is a superuser

        Returns:
            Dictionary with credential data including decrypted credential_data

        Raises:
            ValueError: If credential not found or permission denied
        """
        # Verify credential exists and user owns it
        # Credentials are always private - only owner can access
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if credential.owner_id != owner_id:
            raise ValueError("Not enough permissions")

        # Decrypt the credential data
        credential_data = CredentialsService.decrypt_credential_data(
            session=session,
            credential=credential
        )

        return {
            "id": credential.id,
            "name": credential.name,
            "type": credential.type,
            "notes": credential.notes,
            "allow_sharing": credential.allow_sharing,
            "allow_template_sharing": credential.allow_template_sharing,
            "template_private_fields": list(credential.template_private_fields or []),
            "owner_id": credential.owner_id,
            "user_workspace_id": credential.user_workspace_id,
            "credential_data": credential_data
        }

    @staticmethod
    def find_match_for_spec(
        session: Session,
        user_id: uuid.UUID,
        name: str,
        credential_type: str,
        *,
        fall_back_to_type_only: bool = True,
        template_data: dict | None = None,
        template_private_fields: list[str] | None = None,
    ) -> Credential | None:
        """Suggest an existing credential matching the spec for the user.

        Used by ``CatalogService.build_install_context`` to populate
        ``suggested_credential_id`` on each spec on the install screen.
        Suggestion-only — never auto-commits.

        Match precedence (default / PBU path, ``template_data is None``):
          1. Owned + case-insensitive name match + exact type match.
          2. Shared + case-insensitive name match + exact type match.
          3. Type-only fallback (when ``fall_back_to_type_only=True``): if
             the user has exactly one owned credential of the matching type
             we return it. Two or more type matches return ``None`` so the
             UI shows the manual dropdown instead of guessing.

        The type-only tier is intentionally owned-only — picking an
        ambiguous shared credential by type alone would be too aggressive.
        Within each tier we order by descending ``id`` (a proxy for most
        recent, since ``Credential`` has no ``updated_at`` column) but
        the unique-match rule for the type-only tier short-circuits that.

        PBT-strict path (``template_data is not None``):
          Same owned-then-shared name+type lookup, but each candidate's
          decrypted ``credential_data`` (with ``template_private_fields``
          stripped) must exactly equal ``template_data`` (also stripped
          of those private keys for symmetry). The type-only fallback is
          disabled — a value-anchored match is required, so an ambiguous
          type-only hit must not silently auto-link a user credential
          pointing at a different URL/database than the publisher's
          template specifies. Candidates whose data fails to decrypt are
          skipped rather than raising.

        Returns ``None`` when no match is found.
        """
        from app.models.credentials.credential import CredentialType
        from app.models.credentials.credential_share import CredentialShare

        # Spec ``credential_type`` arrives as a raw string from the
        # revision JSON; map it to the enum so the comparison hits the
        # indexed column. Bail out early on unknown types — no match is
        # possible.
        try:
            type_enum = CredentialType(credential_type)
        except ValueError:
            return None

        pbt_strict = template_data is not None
        private_keys = set(template_private_fields or [])
        spec_stripped = {
            k: v for k, v in (template_data or {}).items() if k not in private_keys
        }

        def _matches_template_data(candidate: Credential) -> bool:
            try:
                data = CredentialsService.decrypt_credential_data(
                    session=session, credential=candidate
                )
            except Exception:
                return False
            candidate_stripped = {
                k: v for k, v in data.items() if k not in private_keys
            }
            return candidate_stripped == spec_stripped

        # Owned credentials first (preferred tier).
        owned_stmt = (
            select(Credential)
            .where(
                Credential.owner_id == user_id,
                Credential.type == type_enum,
                func_sql.lower(Credential.name) == name.lower(),
            )
            .order_by(Credential.id.desc())
        )
        if pbt_strict:
            for candidate in session.exec(owned_stmt).all():
                if _matches_template_data(candidate):
                    return candidate
        else:
            owned_match = session.exec(owned_stmt).first()
            if owned_match is not None:
                return owned_match

        # Shared credentials (fallback tier).
        shared_stmt = (
            select(Credential)
            .join(
                CredentialShare,
                CredentialShare.credential_id == Credential.id,
            )
            .where(
                CredentialShare.shared_with_user_id == user_id,
                Credential.type == type_enum,
                func_sql.lower(Credential.name) == name.lower(),
            )
            .order_by(Credential.id.desc())
        )
        if pbt_strict:
            for candidate in session.exec(shared_stmt).all():
                if _matches_template_data(candidate):
                    return candidate
            # Skip type-only fallback for PBT — value-anchored match required.
            return None
        else:
            shared_match = session.exec(shared_stmt).first()
            if shared_match is not None:
                return shared_match

        if not fall_back_to_type_only:
            return None

        # Type-only fallback — owned credentials only, unique-match required.
        type_only_stmt = (
            select(Credential)
            .where(
                Credential.owner_id == user_id,
                Credential.type == type_enum,
            )
            .order_by(Credential.id.desc())
            .limit(2)
        )
        type_only_matches = list(session.exec(type_only_stmt).all())
        if len(type_only_matches) == 1:
            return type_only_matches[0]
        return None

    @staticmethod
    def get_agent_credentials(
        session: Session,
        agent_id: uuid.UUID
    ) -> list[Credential]:
        """
        Get all credentials linked to an agent.

        Args:
            session: Database session
            agent_id: Agent ID

        Returns:
            List of Credential models
        """
        statement = (
            select(Credential)
            .join(AgentCredentialLink)
            .where(AgentCredentialLink.agent_id == agent_id)
        )
        return list(session.exec(statement).all())

    @staticmethod
    async def link_credential_to_agent(
        session: Session,
        agent_id: uuid.UUID,
        credential_id: uuid.UUID,
        owner_id: uuid.UUID,
        is_superuser: bool = False
    ):
        """
        Link a credential to an agent with authorization checks.

        Users can link credentials they own OR credentials shared with them.

        Args:
            session: Database session
            agent_id: Agent ID
            credential_id: Credential ID
            owner_id: User ID making the request
            is_superuser: Whether the user is a superuser

        Raises:
            ValueError: If agent or credential not found, or permission denied
        """
        from app.services.credentials.credential_share_service import CredentialShareService

        # Verify agent exists and user owns it
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")
        if not is_superuser and agent.owner_id != owner_id:
            raise ValueError("Not enough permissions to access this agent")

        # Verify credential exists and user can access it (owns it OR has share)
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if not CredentialShareService.can_user_access_credential(session, credential_id, owner_id):
            raise ValueError("Not enough permissions to access this credential")

        # Link credential to agent (idempotent)
        existing_link = session.exec(
            select(AgentCredentialLink).where(
                AgentCredentialLink.agent_id == agent_id,
                AgentCredentialLink.credential_id == credential_id,
            )
        ).first()
        if not existing_link:
            session.add(AgentCredentialLink(agent_id=agent_id, credential_id=credential_id))
            session.commit()

        # Sync to running environments
        await CredentialsService.event_credential_shared(
            session=session,
            agent_id=agent_id,
            credential_id=credential_id
        )

    @staticmethod
    async def unlink_credential_from_agent(
        session: Session,
        agent_id: uuid.UUID,
        credential_id: uuid.UUID,
        owner_id: uuid.UUID,
        is_superuser: bool = False
    ):
        """
        Unlink a credential from an agent with authorization checks.

        Args:
            session: Database session
            agent_id: Agent ID
            credential_id: Credential ID
            owner_id: User ID making the request
            is_superuser: Whether the user is a superuser

        Raises:
            ValueError: If agent not found or permission denied
        """
        # Verify agent exists and user owns it
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")
        if not is_superuser and agent.owner_id != owner_id:
            raise ValueError("Not enough permissions to access this agent")

        # Unlink credential from agent
        link = session.exec(
            select(AgentCredentialLink).where(
                AgentCredentialLink.agent_id == agent_id,
                AgentCredentialLink.credential_id == credential_id,
            )
        ).first()
        if link:
            session.delete(link)
            session.commit()

        # Sync to running environments
        await CredentialsService.event_credential_unshared(
            session=session,
            agent_id=agent_id,
            credential_id=credential_id
        )

    @staticmethod
    def get_affected_agents(
        session: Session,
        credential_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """
        Get all agent IDs that have this credential linked.

        Args:
            session: Database session
            credential_id: Credential ID

        Returns:
            List of agent UUIDs
        """
        from app.models.credentials.link_models import AgentCredentialLink

        statement = select(AgentCredentialLink.agent_id).where(
            AgentCredentialLink.credential_id == credential_id
        )
        agent_ids = session.exec(statement).all()
        return list(agent_ids)

    @staticmethod
    async def event_credential_updated(
        session: Session,
        credential_id: uuid.UUID
    ):
        """
        Event handler for when a credential is updated.

        Syncs credentials to all running environments of affected agents.

        Args:
            session: Database session
            credential_id: Updated credential ID
        """
        logger.info(f"Credential {credential_id} updated, syncing to affected agents")

        # Get all agents that use this credential
        agent_ids = CredentialsService.get_affected_agents(session, credential_id)

        if not agent_ids:
            logger.info(f"No agents using credential {credential_id}")
            return

        logger.info(f"Credential {credential_id} affects {len(agent_ids)} agent(s)")

        # Sync to each agent's running environments
        for agent_id in agent_ids:
            await CredentialsService.sync_credentials_to_agent_environments(
                session=session,
                agent_id=agent_id
            )

    @staticmethod
    async def event_credential_deleted(
        session: Session,
        credential_id: uuid.UUID,
        agent_ids: list[uuid.UUID]
    ):
        """
        Event handler for when a credential is deleted.

        Note: agent_ids must be collected BEFORE the credential is deleted
        since the links will be cascade deleted.

        Args:
            session: Database session
            credential_id: Deleted credential ID
            agent_ids: List of agent IDs that were affected (collected before deletion)
        """
        logger.info(f"Credential {credential_id} deleted, syncing to {len(agent_ids)} affected agent(s)")

        # Sync to each agent's running environments
        for agent_id in agent_ids:
            await CredentialsService.sync_credentials_to_agent_environments(
                session=session,
                agent_id=agent_id
            )

    @staticmethod
    async def event_credential_shared(
        session: Session,
        agent_id: uuid.UUID,
        credential_id: uuid.UUID
    ):
        """
        Event handler for when a credential is shared with an agent.

        Args:
            session: Database session
            agent_id: Agent ID that received the credential
            credential_id: Credential ID that was shared
        """
        logger.info(f"Credential {credential_id} shared with agent {agent_id}")

        # Sync to agent's running environments
        await CredentialsService.sync_credentials_to_agent_environments(
            session=session,
            agent_id=agent_id
        )

    @staticmethod
    async def event_credential_unshared(
        session: Session,
        agent_id: uuid.UUID,
        credential_id: uuid.UUID
    ):
        """
        Event handler for when a credential is unshared from an agent.

        Args:
            session: Database session
            agent_id: Agent ID that lost the credential
            credential_id: Credential ID that was unshared
        """
        logger.info(f"Credential {credential_id} unshared from agent {agent_id}")

        # Sync to agent's running environments
        await CredentialsService.sync_credentials_to_agent_environments(
            session=session,
            agent_id=agent_id
        )

    # OAuth credential types that have refresh tokens and expiration
    OAUTH_CREDENTIAL_TYPES = {
        "gmail_oauth",
        "gmail_oauth_readonly",
        "gdrive_oauth",
        "gdrive_oauth_readonly",
        "gcalendar_oauth",
        "gcalendar_oauth_readonly",
    }

    # Required fields for each credential type to be considered "complete"
    # Note: api_token has conditional requirements handled in check_credential_completeness
    REQUIRED_FIELDS = {
        "email_imap": ["host", "port", "login", "password"],
        "odoo": ["url", "database_name", "login", "api_token"],
        "gmail_oauth": ["access_token"],
        "gmail_oauth_readonly": ["access_token"],
        "gdrive_oauth": ["access_token"],
        "gdrive_oauth_readonly": ["access_token"],
        "gcalendar_oauth": ["access_token"],
        "gcalendar_oauth_readonly": ["access_token"],
        "api_token": ["api_token"],  # Base requirement; api_token_template required only for custom type
        "google_service_account": ["type", "project_id", "private_key", "client_email"],
        "ssh_key": ["public_key", "private_key", "fingerprint", "key_type"],
    }

    @staticmethod
    def check_credential_completeness(credential_type: str, credential_data: dict | None) -> str:
        """
        Check if a credential has all required fields populated.

        Args:
            credential_type: Type of credential (email_imap, odoo, gmail_oauth, etc.)
            credential_data: Decrypted credential data dictionary

        Returns:
            "complete" if all required fields are present and non-empty,
            "incomplete" otherwise
        """
        if not credential_data:
            return "incomplete"

        required_fields = CredentialsService.REQUIRED_FIELDS.get(credential_type, [])

        if not required_fields:
            # Unknown credential type - assume complete if it has any data
            return "complete" if credential_data else "incomplete"

        # Special handling for api_token: custom type requires api_token_template
        if credential_type == "api_token":
            api_token_type = credential_data.get("api_token_type", "bearer")
            if api_token_type == "custom":
                required_fields = ["api_token", "api_token_template"]

        for field in required_fields:
            value = credential_data.get(field)
            # Check if field exists and has a non-empty value
            if value is None or value == "":
                return "incomplete"

        return "complete"

    # Threshold for refreshing credentials before streaming (10 minutes)
    CREDENTIAL_REFRESH_THRESHOLD_SECONDS = 10 * 60

    @staticmethod
    async def refresh_expiring_credentials_for_agent(
        session: Session,
        agent_id: uuid.UUID
    ) -> bool:
        """
        Check and refresh OAuth credentials that are expiring soon for an agent.

        This method is called before initiating a stream to ensure all OAuth
        credentials shared with the agent have valid access tokens for the
        expected duration of the stream (up to 10 minutes).

        Args:
            session: Database session
            agent_id: Agent ID to check credentials for

        Returns:
            True if any credentials were refreshed, False otherwise
        """
        from datetime import datetime, timezone
        from app.services.credentials.oauth_credentials_service import OAuthCredentialsService

        credentials_refreshed = False
        now = datetime.now(timezone.utc).timestamp()
        threshold = now + CredentialsService.CREDENTIAL_REFRESH_THRESHOLD_SECONDS

        # Get all credentials linked to this agent
        credentials = CredentialsService.get_agent_credentials(session=session, agent_id=agent_id)

        if not credentials:
            logger.debug(f"No credentials linked to agent {agent_id}")
            return False

        for credential in credentials:
            # Only check OAuth credential types
            if credential.type.value not in CredentialsService.OAUTH_CREDENTIAL_TYPES:
                continue

            try:
                # Decrypt credential data to check expiration
                credential_data = CredentialsService.decrypt_credential_data(
                    session=session,
                    credential=credential
                )

                expires_at = credential_data.get("expires_at")
                if expires_at is None:
                    logger.warning(
                        f"OAuth credential {credential.id} has no expires_at field, "
                        f"skipping refresh check"
                    )
                    continue

                # Check if credential expires within threshold
                if expires_at <= threshold:
                    time_until_expiry = expires_at - now
                    logger.info(
                        f"Credential {credential.id} ({credential.type.value}) expires in "
                        f"{time_until_expiry:.0f} seconds, refreshing..."
                    )

                    try:
                        # Refresh the credential
                        await OAuthCredentialsService.refresh_oauth_token(
                            session=session,
                            credential=credential
                        )
                        credentials_refreshed = True
                        logger.info(f"Successfully refreshed credential {credential.id}")
                    except ValueError as ve:
                        # No refresh token available
                        logger.warning(
                            f"Cannot refresh credential {credential.id}: {ve}. "
                            f"User may need to re-authorize."
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to refresh credential {credential.id}: {e}",
                            exc_info=True
                        )
                else:
                    time_until_expiry = expires_at - now
                    logger.debug(
                        f"Credential {credential.id} ({credential.type.value}) is valid for "
                        f"{time_until_expiry:.0f} more seconds, no refresh needed"
                    )

            except Exception as e:
                logger.error(
                    f"Error checking credential {credential.id}: {e}",
                    exc_info=True
                )

        return credentials_refreshed

    @staticmethod
    def list_bundle_usages(
        session: Session,
        *,
        credential_id: uuid.UUID,
        requester_id: uuid.UUID,
    ) -> list:
        """List bundles whose publisher install has this credential linked.

        Owner-only: raises ``ValueError`` with ``"Credential not found"``
        when the credential does not exist OR the requester is not the
        owner. The route maps that to HTTP 404 (we don't differentiate
        not-found from not-owned to avoid leaking credential existence).

        ``provided_by`` on each entry is resolved via
        ``PublishService.resolve_provided_by`` so the result matches what
        the publish-time spec collector would emit for the same bundle.
        """
        from app.models import (
            Agent,
            CredentialBundleUsage,
        )
        from app.models.bundles.agent_bundle import AgentBundle
        from app.services.bundles.publish_service import PublishService

        credential = session.get(Credential, credential_id)
        if not credential or credential.owner_id != requester_id:
            raise ValueError("Credential not found")

        stmt = (
            select(AgentBundle, Agent)
            .join(
                Agent,
                (Agent.bundle_uuid == AgentBundle.id)
                & (Agent.is_publisher_install == True),  # noqa: E712
            )
            .join(
                AgentCredentialLink,
                AgentCredentialLink.agent_id == Agent.id,
            )
            .where(AgentCredentialLink.credential_id == credential_id)
        )
        rows = session.exec(stmt).all()

        seen: set[uuid.UUID] = set()
        usages: list[CredentialBundleUsage] = []
        for bundle, publisher_install in rows:
            if bundle.id in seen:
                continue
            seen.add(bundle.id)
            usages.append(
                CredentialBundleUsage(
                    bundle_uuid=bundle.id,
                    bundle_id=bundle.bundle_id,
                    display_name=bundle.display_name,
                    publisher_install_id=publisher_install.id,
                    provided_by=PublishService.resolve_provided_by(
                        credential, publisher_install
                    ),
                )
            )
        return usages
