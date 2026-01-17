"""
Agent Clone Service - Handles creating clones of shared agents.

This service manages:
- Creating clone agent records
- Creating environments for clones
- Copying workspace files (scripts, docs, knowledge)
- Setting up credentials (shared or placeholders)
- Detaching clones from parent
"""
from datetime import datetime
from uuid import UUID
import shutil
import logging
from pathlib import Path

from sqlmodel import Session, select
from fastapi import HTTPException

from app.models.agent import Agent
from app.models.credential import Credential
from app.models.credential_share import CredentialShare
from app.models.environment import AgentEnvironment, AgentEnvironmentCreate
from app.models.link_models import AgentCredentialLink
from app.models.agent_share import AgentShare
from app.core.config import settings
from app.services.environment_service import EnvironmentService
from app.services.ai_credentials_service import ai_credentials_service

logger = logging.getLogger(__name__)


class AgentCloneService:
    """Service for creating and managing agent clones."""

    @staticmethod
    async def create_clone(
        session: Session,
        original_agent: Agent,
        recipient_id: UUID,
        clone_mode: str,
        credentials_data: dict | None = None,
        share: AgentShare | None = None,
        ai_credential_selections: dict | None = None
    ) -> Agent:
        """
        Create a clone of an agent for a recipient.

        Steps:
        1. Create Agent record with clone fields
        2. Create Environment for clone
        3. Copy workspace files from original environment
        4. Setup credentials (link shared, create placeholders)
        5. Setup AI credentials (from share or user's own)
        6. Return clone

        Args:
            session: Database session
            original_agent: The agent being cloned
            recipient_id: UUID of the user receiving the clone
            clone_mode: "user" or "builder"
            credentials_data: Optional dict of {credential_id: {field: value}} for placeholders
            share: Optional AgentShare with AI credential provision info
            ai_credential_selections: Optional dict with conversation_credential_id/building_credential_id

        Returns:
            The created clone Agent
        """
        # 1. Create clone Agent record
        clone = Agent(
            owner_id=recipient_id,
            name=original_agent.name,
            description=original_agent.description,
            workflow_prompt=original_agent.workflow_prompt,
            entrypoint_prompt=original_agent.entrypoint_prompt,
            ui_color_preset=original_agent.ui_color_preset,
            conversation_mode_ui=original_agent.conversation_mode_ui,
            agent_sdk_config=original_agent.agent_sdk_config.copy() if original_agent.agent_sdk_config else {},
            a2a_config=original_agent.a2a_config.copy() if original_agent.a2a_config else {},
            user_workspace_id=None,  # Clones don't inherit workspace

            # Clone-specific fields
            is_clone=True,
            parent_agent_id=original_agent.id,
            clone_mode=clone_mode,
            last_sync_at=datetime.utcnow(),
            update_mode="automatic",
            pending_update=False
        )

        # Handle duplicate name
        clone.name = await AgentCloneService._ensure_unique_name(
            session, recipient_id, clone.name
        )

        session.add(clone)
        session.commit()
        session.refresh(clone)

        # 2. Create environment for clone
        # We need to get the original's active environment to get its config
        original_env = None
        if original_agent.active_environment_id:
            original_env = session.get(AgentEnvironment, original_agent.active_environment_id)

        # Create environment with same template as original
        env_name = settings.DEFAULT_AGENT_ENV_NAME
        env_version = original_env.env_version if original_env else settings.DEFAULT_AGENT_ENV_VERSION
        sdk_conversation = original_env.agent_sdk_conversation if original_env else None
        # Only include building SDK if clone_mode is "builder" (user mode doesn't need building)
        sdk_building = original_env.agent_sdk_building if original_env and clone_mode == "builder" else None

        # Determine AI credential configuration for environment
        # Always use use_default_ai_credentials=False for clones so that EnvironmentService
        # uses the named credentials table (ai_credential) with proper fallback to defaults,
        # rather than the old profile-based credentials (ai_credentials_encrypted)
        use_default_ai_credentials = False
        conversation_ai_credential_id = None
        building_ai_credential_id = None

        # If owner provided AI credentials via the share
        if share and share.provide_ai_credentials:
            # Create AI credential shares for the recipient
            if share.conversation_ai_credential_id:
                ai_credentials_service.share_credential(
                    session=session,
                    credential_id=share.conversation_ai_credential_id,
                    owner_id=share.shared_by_user_id,
                    recipient_id=recipient_id
                )
                conversation_ai_credential_id = share.conversation_ai_credential_id

            # Only set building credentials if clone_mode is "builder"
            if clone_mode == "builder" and share.building_ai_credential_id:
                ai_credentials_service.share_credential(
                    session=session,
                    credential_id=share.building_ai_credential_id,
                    owner_id=share.shared_by_user_id,
                    recipient_id=recipient_id
                )
                building_ai_credential_id = share.building_ai_credential_id
        elif ai_credential_selections:
            # User provided their own credential selections
            selected_conv = ai_credential_selections.get("conversation_credential_id")
            selected_build = ai_credential_selections.get("building_credential_id") if clone_mode == "builder" else None
            conversation_ai_credential_id = selected_conv
            building_ai_credential_id = selected_build
        # If neither owner provided nor user selected, leave credential IDs as None
        # EnvironmentService will fall back to recipient's default named credentials

        env_data = AgentEnvironmentCreate(
            env_name=env_name,
            env_version=env_version,
            instance_name="Default",
            type="docker",
            config={},
            agent_sdk_conversation=sdk_conversation,
            agent_sdk_building=sdk_building,
            use_default_ai_credentials=use_default_ai_credentials,
            conversation_ai_credential_id=conversation_ai_credential_id,
            building_ai_credential_id=building_ai_credential_id
        )

        # Get recipient user for environment creation
        from app.models.user import User
        recipient_user = session.get(User, recipient_id)

        clone_env = None
        try:
            clone_env = await EnvironmentService.create_environment(
                session=session,
                agent_id=clone.id,
                data=env_data,
                user=recipient_user,
                auto_start=False  # Don't auto-start until credentials are set up
            )
            logger.info(f"Created environment {clone_env.id} for clone {clone.id}")

            # Set the active environment on the clone
            clone.active_environment_id = clone_env.id
            session.add(clone)
            session.commit()
            session.refresh(clone)
        except Exception as e:
            logger.error(f"Failed to create environment for clone: {e}")
            # Continue without environment - it can be created later

        # 3. Copy workspace files from original to clone after env is created
        if original_env and clone_env:
            await AgentCloneService.copy_workspace(
                original_env_id=original_env.id,
                clone_env_id=clone_env.id
            )

        # 4. Setup credentials
        await AgentCloneService.setup_clone_credentials(
            session=session,
            original_agent=original_agent,
            clone=clone,
            user_provided_data=credentials_data or {}
        )

        return clone

    @staticmethod
    async def _ensure_unique_name(
        session: Session,
        owner_id: UUID,
        name: str
    ) -> str:
        """Ensure agent name is unique for the owner."""
        original_name = name
        counter = 1

        while True:
            stmt = select(Agent).where(
                Agent.owner_id == owner_id,
                Agent.name == name
            )
            existing = session.exec(stmt).first()
            if not existing:
                return name
            counter += 1
            name = f"{original_name} ({counter})"

    @staticmethod
    async def copy_workspace(
        original_env_id: UUID,
        clone_env_id: UUID
    ) -> None:
        """
        Copy workspace files from original environment to clone.

        Copies:
        - app/core/scripts/ (all agent scripts)
        - app/core/docs/ (WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md)
        - app/core/knowledge/ (integration docs if any)

        Does NOT copy:
        - User data files
        - Databases
        - Logs
        - Credentials (handled separately)
        """
        # Get workspace paths
        instances_dir = Path(settings.ENV_INSTANCES_DIR)
        original_workspace = instances_dir / str(original_env_id)
        clone_workspace = instances_dir / str(clone_env_id)

        if not original_workspace.exists():
            logger.warning(f"Original workspace not found: {original_workspace}")
            return

        if not clone_workspace.exists():
            logger.warning(f"Clone workspace not found: {clone_workspace}")
            return

        # Directories to copy (relative to app/core/)
        dirs_to_copy = [
            ("app/core/scripts", "app/core/scripts"),
            ("app/core/docs", "app/core/docs"),
            ("app/core/knowledge", "app/core/knowledge"),
        ]

        for src_rel, dst_rel in dirs_to_copy:
            src = original_workspace / src_rel
            dst = clone_workspace / dst_rel

            if src.exists():
                try:
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(src, dst)
                    logger.info(f"Copied {src_rel} to clone workspace")
                except Exception as e:
                    logger.error(f"Failed to copy {src_rel}: {e}")

    @staticmethod
    async def setup_clone_credentials(
        session: Session,
        original_agent: Agent,
        clone: Agent,
        user_provided_data: dict
    ) -> list[Credential]:
        """
        Setup credentials for a clone.

        For each credential linked to original agent:
        - If allow_sharing=true: Create CredentialShare link
        - If allow_sharing=false: Create placeholder credential

        Apply user_provided_data to placeholders.

        Args:
            session: Database session
            original_agent: The original agent
            clone: The cloned agent
            user_provided_data: Dict of {credential_id: {field: value}} for placeholders

        Returns:
            List of created/linked credentials
        """
        from app.services.credentials_service import CredentialsService

        # Get original agent's credentials via the link table
        stmt = select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == original_agent.id
        )
        links = list(session.exec(stmt).all())

        created_credentials = []

        for link in links:
            orig_cred = session.get(Credential, link.credential_id)
            if not orig_cred:
                continue

            if orig_cred.allow_sharing:
                # Create CredentialShare link for recipient
                # Check if share already exists
                stmt = select(CredentialShare).where(
                    CredentialShare.credential_id == orig_cred.id,
                    CredentialShare.shared_with_user_id == clone.owner_id
                )
                existing_share = session.exec(stmt).first()

                if not existing_share:
                    share = CredentialShare(
                        credential_id=orig_cred.id,
                        shared_with_user_id=clone.owner_id,
                        shared_by_user_id=original_agent.owner_id
                    )
                    session.add(share)
                    logger.info(f"Created credential share for {orig_cred.name}")

                # Link the shared credential to the clone
                clone_link = AgentCredentialLink(
                    agent_id=clone.id,
                    credential_id=orig_cred.id
                )
                session.add(clone_link)

            else:
                # Create placeholder credential for the clone
                # Import encryption function
                from app.services.credentials_service import CredentialsService

                placeholder = Credential(
                    owner_id=clone.owner_id,
                    name=f"{orig_cred.name} (placeholder)",
                    type=orig_cred.type,
                    notes=f"Placeholder for shared credential. Please configure with your own values.",
                    encrypted_data=CredentialsService._encrypt_data({}),  # Empty encrypted data
                    is_placeholder=True,
                    placeholder_source_id=orig_cred.id,
                    allow_sharing=False
                )

                # Apply user-provided data if available
                cred_key = str(orig_cred.id)
                if cred_key in user_provided_data:
                    placeholder.encrypted_data = CredentialsService._encrypt_data(
                        user_provided_data[cred_key]
                    )
                    placeholder.is_placeholder = False  # Now complete
                    placeholder.name = orig_cred.name  # Use original name

                session.add(placeholder)
                session.flush()  # Get the ID

                # Link placeholder to clone
                clone_link = AgentCredentialLink(
                    agent_id=clone.id,
                    credential_id=placeholder.id
                )
                session.add(clone_link)
                created_credentials.append(placeholder)
                logger.info(f"Created placeholder credential for {orig_cred.name}")

        session.commit()
        return created_credentials

    @staticmethod
    async def detach_clone(
        session: Session,
        clone_id: UUID,
        clone_owner_id: UUID
    ) -> Agent:
        """
        Detach a clone from its parent.
        Clone becomes a regular independent agent.

        Args:
            session: Database session
            clone_id: The clone agent ID
            clone_owner_id: The owner of the clone

        Returns:
            The updated Agent (now independent)
        """
        clone = session.get(Agent, clone_id)
        if not clone:
            raise HTTPException(status_code=404, detail="Agent not found")

        if clone.owner_id != clone_owner_id:
            raise HTTPException(status_code=403, detail="Not your agent")

        if not clone.is_clone:
            raise HTTPException(status_code=400, detail="Agent is not a clone")

        # Detach from parent
        clone.is_clone = False
        clone.parent_agent_id = None
        clone.clone_mode = None
        clone.pending_update = False
        clone.pending_update_at = None

        session.add(clone)
        session.commit()
        session.refresh(clone)

        logger.info(f"Clone {clone_id} detached from parent, now independent")
        return clone

    @staticmethod
    async def sync_workspace_from_parent(
        session: Session,
        clone: Agent
    ) -> None:
        """
        Sync workspace files from parent to clone.
        Used when applying updates from parent.

        Args:
            session: Database session
            clone: The clone agent to update
        """
        if not clone.is_clone or not clone.parent_agent_id:
            return

        parent = session.get(Agent, clone.parent_agent_id)
        if not parent or not parent.active_environment_id:
            return

        if not clone.active_environment_id:
            return

        await AgentCloneService.copy_workspace(
            original_env_id=parent.active_environment_id,
            clone_env_id=clone.active_environment_id
        )

        # Update sync timestamp
        clone.last_sync_at = datetime.utcnow()
        clone.pending_update = False
        clone.pending_update_at = None
        session.add(clone)
        session.commit()

    # ============ PHASE 4: UPDATE MECHANISM ============

    @staticmethod
    async def push_updates(
        session: Session,
        original_agent_id: UUID,
        owner_id: UUID
    ) -> dict:
        """
        Queue updates to all clones of an agent.

        Process:
        1. Verify ownership
        2. Find all clones (parent_agent_id = original_agent_id)
        3. Set pending_update=True for all clones
        4. For automatic mode clones: trigger immediate apply
        5. Return count and details

        Returns: {
            "clones_queued": int,
            "clones_auto_updated": int,
            "clones_pending_manual": int
        }
        """
        # 1. Verify ownership
        original = session.get(Agent, original_agent_id)
        if not original:
            raise HTTPException(status_code=404, detail="Agent not found")
        if original.owner_id != owner_id:
            raise HTTPException(status_code=403, detail="Not your agent")
        if original.is_clone:
            raise HTTPException(status_code=400, detail="Cannot push updates from a clone")

        # 2. Find all clones
        stmt = select(Agent).where(Agent.parent_agent_id == original_agent_id)
        clones = list(session.exec(stmt).all())

        if not clones:
            return {
                "clones_queued": 0,
                "clones_auto_updated": 0,
                "clones_pending_manual": 0
            }

        auto_updated = 0
        pending_manual = 0

        for clone in clones:
            clone.pending_update = True
            clone.pending_update_at = datetime.utcnow()
            session.add(clone)

            if clone.update_mode == "automatic":
                # For MVP: apply immediately
                try:
                    await AgentCloneService._apply_update_internal(
                        session=session,
                        clone=clone,
                        parent=original
                    )
                    auto_updated += 1
                except Exception as e:
                    # Log error, keep pending_update=True
                    logger.error(f"Failed to auto-update clone {clone.id}: {e}")
                    pending_manual += 1
            else:
                pending_manual += 1

        session.commit()

        return {
            "clones_queued": len(clones),
            "clones_auto_updated": auto_updated,
            "clones_pending_manual": pending_manual
        }

    @staticmethod
    async def _apply_update_internal(
        session: Session,
        clone: Agent,
        parent: Agent
    ) -> None:
        """
        Internal method to apply update from parent to clone.
        Called by push_updates for automatic mode clones.
        """
        # Sync workspace files
        if parent.active_environment_id and clone.active_environment_id:
            await AgentCloneService.copy_workspace(
                original_env_id=parent.active_environment_id,
                clone_env_id=clone.active_environment_id
            )

        # Update clone record
        clone.last_sync_at = datetime.utcnow()
        clone.pending_update = False
        clone.pending_update_at = None
        session.add(clone)

    @staticmethod
    async def apply_update(
        session: Session,
        clone_id: UUID,
        clone_owner_id: UUID
    ) -> Agent:
        """
        Apply pending update from parent to clone (manual trigger).

        Process:
        1. Get clone and parent
        2. Verify pending update exists
        3. Sync workspace files
        4. Update last_sync_at
        5. Clear pending_update flag

        Returns: Updated clone
        """
        clone = session.get(Agent, clone_id)
        if not clone:
            raise HTTPException(status_code=404, detail="Agent not found")
        if clone.owner_id != clone_owner_id:
            raise HTTPException(status_code=403, detail="Not your agent")
        if not clone.is_clone:
            raise HTTPException(status_code=400, detail="Agent is not a clone")
        if not clone.parent_agent_id:
            raise HTTPException(status_code=400, detail="Clone has no parent (detached)")
        if not clone.pending_update:
            raise HTTPException(status_code=400, detail="No pending update")

        parent = session.get(Agent, clone.parent_agent_id)
        if not parent:
            raise HTTPException(
                status_code=404,
                detail="Parent agent no longer exists. Consider detaching this clone."
            )

        # Sync workspace files
        if parent.active_environment_id and clone.active_environment_id:
            await AgentCloneService.copy_workspace(
                original_env_id=parent.active_environment_id,
                clone_env_id=clone.active_environment_id
            )

        # Update clone record
        clone.last_sync_at = datetime.utcnow()
        clone.pending_update = False
        clone.pending_update_at = None
        session.add(clone)
        session.commit()
        session.refresh(clone)

        logger.info(f"Applied update to clone {clone_id} from parent {parent.id}")
        return clone

    @staticmethod
    async def get_update_status(
        session: Session,
        clone_id: UUID,
        clone_owner_id: UUID
    ) -> dict:
        """
        Get update status for a clone.

        Returns: {
            "has_pending_update": bool,
            "pending_since": datetime | None,
            "last_sync_at": datetime | None,
            "update_mode": str,
            "parent_exists": bool,
            "parent_name": str | None
        }
        """
        clone = session.get(Agent, clone_id)
        if not clone:
            raise HTTPException(status_code=404, detail="Agent not found")
        if clone.owner_id != clone_owner_id:
            raise HTTPException(status_code=403, detail="Not your agent")
        if not clone.is_clone:
            return {
                "has_pending_update": False,
                "pending_since": None,
                "last_sync_at": None,
                "update_mode": None,
                "parent_exists": False,
                "parent_name": None
            }

        parent = None
        if clone.parent_agent_id:
            parent = session.get(Agent, clone.parent_agent_id)

        return {
            "has_pending_update": clone.pending_update,
            "pending_since": clone.pending_update_at,
            "last_sync_at": clone.last_sync_at,
            "update_mode": clone.update_mode,
            "parent_exists": parent is not None,
            "parent_name": parent.name if parent else None
        }

    @staticmethod
    async def set_update_mode(
        session: Session,
        clone_id: UUID,
        clone_owner_id: UUID,
        update_mode: str
    ) -> Agent:
        """
        Set update mode for a clone.

        update_mode: "automatic" | "manual"
        """
        if update_mode not in ["automatic", "manual"]:
            raise HTTPException(status_code=400, detail="Invalid update mode")

        clone = session.get(Agent, clone_id)
        if not clone:
            raise HTTPException(status_code=404, detail="Agent not found")
        if clone.owner_id != clone_owner_id:
            raise HTTPException(status_code=403, detail="Not your agent")
        if not clone.is_clone:
            raise HTTPException(status_code=400, detail="Agent is not a clone")

        clone.update_mode = update_mode
        session.add(clone)
        session.commit()
        session.refresh(clone)

        logger.info(f"Set update mode for clone {clone_id} to {update_mode}")
        return clone
