"""
Credentials Service - Business logic for credential operations.
"""
import uuid
import json
import copy
import logging
from sqlmodel import Session, select
from app.models import Credential, Agent, AgentEnvironment, CredentialCreate, CredentialUpdate
from app import crud

logger = logging.getLogger(__name__)


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
        "odoo": ["api_token"],
        "gmail_oauth": ["access_token", "refresh_token"],
        "gmail_oauth_readonly": ["access_token", "refresh_token"],
        "gdrive_oauth": ["access_token", "refresh_token"],
        "gdrive_oauth_readonly": ["access_token", "refresh_token"],
        "gcalendar_oauth": ["access_token", "refresh_token"],
        "gcalendar_oauth_readonly": ["access_token", "refresh_token"],
        "api_token": ["http_header_value"],
    }

    # WHITELIST: Fields that ARE allowed to be exposed to agent environment
    # Security: Only explicitly listed fields are transferred. Any field not listed is excluded.
    # This prevents accidental exposure of new sensitive fields (refresh_token, client_secret, etc.)
    AGENT_ENV_ALLOWED_FIELDS = {
        # Non-OAuth credentials: Need all functional fields for agent to use them
        "email_imap": ["host", "port", "login", "password", "is_ssl"],
        "odoo": ["url", "database_name", "login", "api_token"],
        "api_token": ["http_header_name", "http_header_value"],

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
    }

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
        credentials = crud.get_agent_credentials(session=session, agent_id=agent_id)

        result = []
        for cred in credentials:
            # Decrypt credential data
            credential_data = crud.get_credential_with_data(session=session, credential=cred)

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
            "credentials_readme": readme_content
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
        from app.services.environment_service import EnvironmentService

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
        return crud.create_credential(
            session=session,
            credential_in=credential_in,
            owner_id=owner_id
        )

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
        credential = crud.update_credential(
            session=session,
            db_credential=credential,
            credential_in=credential_in
        )

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
        credential_data = crud.get_credential_with_data(
            session=session,
            credential=credential
        )

        return {
            "id": credential.id,
            "name": credential.name,
            "type": credential.type,
            "notes": credential.notes,
            "allow_sharing": credential.allow_sharing,
            "owner_id": credential.owner_id,
            "user_workspace_id": credential.user_workspace_id,
            "credential_data": credential_data
        }

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
        return crud.get_agent_credentials(session=session, agent_id=agent_id)

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
        from app.services.credential_share_service import CredentialShareService

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

        # Link credential to agent
        crud.add_credential_to_agent(
            session=session,
            agent_id=agent_id,
            credential_id=credential_id
        )

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
        crud.remove_credential_from_agent(
            session=session,
            agent_id=agent_id,
            credential_id=credential_id
        )

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
        from app.models.link_models import AgentCredentialLink

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
    REQUIRED_FIELDS = {
        "email_imap": ["host", "port", "login", "password"],
        "odoo": ["url", "database_name", "login", "api_token"],
        "gmail_oauth": ["access_token"],
        "gmail_oauth_readonly": ["access_token"],
        "gdrive_oauth": ["access_token"],
        "gdrive_oauth_readonly": ["access_token"],
        "gcalendar_oauth": ["access_token"],
        "gcalendar_oauth_readonly": ["access_token"],
        "api_token": ["api_token_type", "api_token"],
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
        from app.services.oauth_credentials_service import OAuthCredentialsService
        from app import crud

        credentials_refreshed = False
        now = datetime.now(timezone.utc).timestamp()
        threshold = now + CredentialsService.CREDENTIAL_REFRESH_THRESHOLD_SECONDS

        # Get all credentials linked to this agent
        credentials = crud.get_agent_credentials(session=session, agent_id=agent_id)

        if not credentials:
            logger.debug(f"No credentials linked to agent {agent_id}")
            return False

        for credential in credentials:
            # Only check OAuth credential types
            if credential.type.value not in CredentialsService.OAUTH_CREDENTIAL_TYPES:
                continue

            try:
                # Decrypt credential data to check expiration
                credential_data = crud.get_credential_with_data(
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
