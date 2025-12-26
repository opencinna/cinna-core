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

    # Fields to redact in credentials (by credential type)
    SENSITIVE_FIELDS = {
        "email_imap": ["password"],
        "odoo": ["api_token"],
        "gmail_oauth": ["access_token", "refresh_token"],
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

            result.append({
                "id": str(cred.id),
                "name": cred.name,
                "type": cred.type.value,
                "notes": cred.notes,
                "credential_data": credential_data
            })

        return result

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
            "# Find specific credential by name or type (type preferable)",
            "for cred in all_credentials:",
            "    if cred['type'] == 'email_imap':",
            "        imap_config = cred['credential_data']",
            "        # Use imap_config['host'], imap_config['login'], etc.",
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
            if cred_type == "email_imap":
                lines.append(f"### IMAP Credential: {cred_name}")
                lines.append("")
                lines.append("```python")
                lines.append("import json")
                lines.append("import imaplib")
                lines.append("")
                lines.append("# Load credentials")
                lines.append("with open('credentials/credentials.json', 'r') as f:")
                lines.append("    all_credentials = json.load(f)")
                lines.append("")
                lines.append(f"# Find '{cred_name}' credential")
                lines.append("for cred in all_credentials:")
                lines.append(f"    if cred['name'] == '{cred_name}':")
                lines.append("        config = cred['credential_data']")
                lines.append("        # Connect to IMAP server")
                lines.append("        if config.get('is_ssl', True):")
                lines.append("            mail = imaplib.IMAP4_SSL(config['host'], config['port'])")
                lines.append("        else:")
                lines.append("            mail = imaplib.IMAP4(config['host'], config['port'])")
                lines.append("        mail.login(config['login'], config['password'])")
                lines.append("        # ... use mail connection")
                lines.append("        mail.logout()")
                lines.append("```")
                lines.append("")
            elif cred_type == "odoo":
                lines.append(f"### Odoo Credential: {cred_name}")
                lines.append("")
                lines.append("```python")
                lines.append("import json")
                lines.append("import xmlrpc.client")
                lines.append("")
                lines.append("# Load credentials")
                lines.append("with open('credentials/credentials.json', 'r') as f:")
                lines.append("    all_credentials = json.load(f)")
                lines.append("")
                lines.append(f"# Find '{cred_name}' credential")
                lines.append("for cred in all_credentials:")
                lines.append(f"    if cred['name'] == '{cred_name}':")
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
                lines.append("```")
                lines.append("")
            elif cred_type == "gmail_oauth":
                lines.append(f"### Gmail OAuth Credential: {cred_name}")
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
                lines.append(f"# Find '{cred_name}' credential")
                lines.append("for cred in all_credentials:")
                lines.append(f"    if cred['name'] == '{cred_name}':")
                lines.append("        # Use Gmail API")
                lines.append("        creds = Credentials.from_authorized_user_info(cred['credential_data'])")
                lines.append("        service = build('gmail', 'v1', credentials=creds)")
                lines.append("        # ... use Gmail API")
                lines.append("```")
                lines.append("")

        lines.append("## Best Practices")
        lines.append("")
        lines.append("1. **Load credentials at script start** and reuse the connection")
        lines.append("2. **Handle errors gracefully** - credentials might be invalid or expired")
        lines.append("3. **Close connections properly** when done")
        lines.append("4. **Never hardcode credentials** - always read from the credentials file")
        lines.append("")

        return "\n".join(lines)

    @staticmethod
    def prepare_credentials_for_environment(
        session: Session,
        agent_id: uuid.UUID
    ) -> dict:
        """
        Prepare credentials data for syncing to agent environment.

        Returns:
            Dictionary with two keys:
            - "credentials_json": Full credentials data (for credentials.json file)
            - "credentials_readme": Redacted README content (for credentials/README.md file)
        """
        # Get credentials with decrypted data
        credentials = CredentialsService.get_agent_credentials_with_data(session, agent_id)

        # Generate README with redacted data
        readme_content = CredentialsService.generate_credentials_readme(credentials)

        return {
            "credentials_json": credentials,
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
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if not is_superuser and credential.owner_id != owner_id:
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
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if not is_superuser and credential.owner_id != owner_id:
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
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if not is_superuser and credential.owner_id != owner_id:
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
            "owner_id": credential.owner_id,
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

        Args:
            session: Database session
            agent_id: Agent ID
            credential_id: Credential ID
            owner_id: User ID making the request
            is_superuser: Whether the user is a superuser

        Raises:
            ValueError: If agent or credential not found, or permission denied
        """
        # Verify agent exists and user owns it
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")
        if not is_superuser and agent.owner_id != owner_id:
            raise ValueError("Not enough permissions to access this agent")

        # Verify credential exists and user owns it
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if not is_superuser and credential.owner_id != owner_id:
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
