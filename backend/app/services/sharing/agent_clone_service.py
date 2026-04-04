"""
Agent Clone Service - Handles creating clones of shared agents.

This service manages:
- Creating clone agent records
- Creating environments for clones
- Copying workspace files (scripts, docs, knowledge)
- Setting up credentials (shared or placeholders)
- Detaching clones from parent
"""
from datetime import UTC, datetime
from uuid import UUID
import shutil
import logging
from pathlib import Path

from sqlmodel import Session, select
from fastapi import HTTPException

from app.models.agents.agent import Agent
from app.models.credentials.credential import Credential
from app.models.credentials.credential_share import CredentialShare
from app.models.environments.environment import AgentEnvironment, AgentEnvironmentCreate
from app.models.credentials.link_models import AgentCredentialLink
from app.models.sharing.agent_share import AgentShare
from app.models.sharing.clone_update_request import CloneUpdateRequest, UpdateRequestStatus
from app.core.config import settings
from app.services.environments.environment_service import EnvironmentService
from app.services.credentials.ai_credentials_service import ai_credentials_service

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
            example_prompts=list(original_agent.example_prompts) if original_agent.example_prompts else [],
            user_workspace_id=None,  # Clones don't inherit workspace

            # Clone-specific fields
            is_clone=True,
            parent_agent_id=original_agent.id,
            clone_mode=clone_mode,
            last_sync_at=datetime.now(UTC),
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
        from app.models.users.user import User
        recipient_user = session.get(User, recipient_id)

        clone_env = None
        try:
            clone_env = await EnvironmentService.create_environment(
                session=session,
                agent_id=clone.id,
                data=env_data,
                user=recipient_user,
                auto_start=True,  # Auto-start the environment after build completes
                source_environment_id=original_env.id if original_env else None  # Copy workspace from original
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

        # Note: Workspace files are copied inside _create_environment_background() after the
        # environment directory is created, using the source_environment_id parameter

        # 3. Setup credentials
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
        clone_env_id: UUID,
        include_files_folder: bool = True
    ) -> None:
        """
        Copy workspace files from original environment to clone.

        Standard copy (always included):
        - app/workspace/scripts/ (all agent scripts)
        - app/workspace/docs/ (WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md)
        - app/workspace/knowledge/ (integration docs if any)
        - app/workspace/workspace_requirements.txt (agent-installed Python packages)

        Optional copy (when include_files_folder=True):
        - app/workspace/files/ (generated reports, CSV files, SQLite DBs, local caches)
        - app/workspace/uploads/ (user-uploaded files)

        Does NOT copy (Environment Runtime - not synced):
        - app/workspace/logs/ (session logs)
        - app/workspace/databases/ (runtime SQLite DBs)
        - app/workspace/credentials/ (handled separately via dynamic sync)

        Args:
            original_env_id: Source environment ID
            clone_env_id: Target environment ID
            include_files_folder: Whether to copy files/ and uploads/ directories
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

        # Standard directories to copy (always included)
        dirs_to_copy = [
            ("app/workspace/scripts", "app/workspace/scripts"),
            ("app/workspace/docs", "app/workspace/docs"),
            ("app/workspace/knowledge", "app/workspace/knowledge"),
            ("app/workspace/webapp", "app/workspace/webapp"),
        ]

        # Optional directories (files folder)
        if include_files_folder:
            dirs_to_copy.extend([
                ("app/workspace/files", "app/workspace/files"),  # Reports, caches, CSVs
                ("app/workspace/uploads", "app/workspace/uploads"),  # User-uploaded files
            ])

        # Single files to copy
        files_to_copy = [
            ("app/workspace/workspace_requirements.txt", "app/workspace/workspace_requirements.txt"),
            ("app/workspace/workspace_system_packages.txt", "app/workspace/workspace_system_packages.txt"),
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

        for src_rel, dst_rel in files_to_copy:
            src = original_workspace / src_rel
            dst = clone_workspace / dst_rel

            if src.exists():
                try:
                    # Ensure destination directory exists
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
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
        - If user selected their own credential (by ID): Link that credential
        - If allow_sharing=true and no override: Create CredentialShare link
        - If allow_sharing=false and no selection: Create placeholder credential

        Args:
            session: Database session
            original_agent: The original agent
            clone: The cloned agent
            user_provided_data: Dict of {credential_name: credential_id} for credential selections
                               OR {credential_name: {field: value}} for legacy placeholder data

        Returns:
            List of created/linked credentials
        """
        from app.services.credentials.credentials_service import CredentialsService
        from uuid import UUID

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

            # Check if user provided a credential selection (by name)
            user_selection = user_provided_data.get(orig_cred.name) if user_provided_data else None

            # Determine if user_selection is a credential ID (string UUID) or legacy data (dict)
            selected_credential_id = None
            legacy_data = None

            if user_selection:
                if isinstance(user_selection, str):
                    # New format: credential ID as string
                    try:
                        selected_credential_id = UUID(user_selection)
                    except (ValueError, TypeError):
                        # Not a valid UUID, might be other data
                        pass
                elif isinstance(user_selection, dict):
                    # Legacy format: {field: value} for placeholder data
                    legacy_data = user_selection

            # If user selected their own credential, link it directly
            if selected_credential_id:
                # Verify the credential exists and belongs to or is shared with the clone owner
                selected_cred = session.get(Credential, selected_credential_id)
                if selected_cred:
                    # Check if user owns the credential
                    user_owns = selected_cred.owner_id == clone.owner_id

                    # Check if credential is shared with the user
                    user_has_access = False
                    if not user_owns:
                        stmt = select(CredentialShare).where(
                            CredentialShare.credential_id == selected_credential_id,
                            CredentialShare.shared_with_user_id == clone.owner_id
                        )
                        share_record = session.exec(stmt).first()
                        user_has_access = share_record is not None

                    if user_owns or user_has_access:
                        # Link user's credential to the clone
                        clone_link = AgentCredentialLink(
                            agent_id=clone.id,
                            credential_id=selected_credential_id
                        )
                        session.add(clone_link)
                        logger.info(f"Linked user's credential {selected_cred.name} for {orig_cred.name}")
                        continue
                    else:
                        logger.warning(f"User does not have access to credential {selected_credential_id} for {orig_cred.name}")
                else:
                    logger.warning(f"Credential {selected_credential_id} not found for {orig_cred.name}")

            # Default behavior: share or create placeholder
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

                # Apply legacy user-provided data if available
                if legacy_data:
                    placeholder.encrypted_data = CredentialsService._encrypt_data(legacy_data)
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
        clone.last_update_status = None

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
        clone.last_sync_at = datetime.now(UTC)
        clone.pending_update = False
        clone.pending_update_at = None
        clone.last_update_status = "synced"
        session.add(clone)
        session.commit()

    # ============ PHASE 4: UPDATE MECHANISM ============

    @staticmethod
    async def push_updates(
        session: Session,
        original_agent_id: UUID,
        owner_id: UUID,
        copy_files_folder: bool = False,
        rebuild_environment: bool = False
    ) -> dict:
        """
        Queue updates to all clones of an agent.

        Process:
        1. Verify ownership
        2. Find all clones (parent_agent_id = original_agent_id)
        3. Create CloneUpdateRequest for each clone with the specified actions
        4. Set pending_update=True for all clones
        5. For automatic mode clones: trigger immediate apply
        6. Return count and details

        Args:
            session: Database session
            original_agent_id: The parent agent ID
            owner_id: The owner of the parent agent
            copy_files_folder: Whether to copy the files folder
            rebuild_environment: Whether to rebuild the environment

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
            # 3. Create CloneUpdateRequest record for each clone
            update_request = CloneUpdateRequest(
                clone_agent_id=clone.id,
                parent_agent_id=original_agent_id,
                pushed_by_user_id=owner_id,
                copy_files_folder=copy_files_folder,
                rebuild_environment=rebuild_environment,
                status=UpdateRequestStatus.PENDING
            )
            session.add(update_request)

            # 4. Set pending_update on clone
            clone.pending_update = True
            clone.pending_update_at = datetime.now(UTC)
            session.add(clone)

            # Always queue the request - automatic updates will be applied
            # when the agent environment is inactive (handled by separate logic)
            if clone.update_mode == "automatic":
                auto_updated += 1
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
        clone.last_sync_at = datetime.now(UTC)
        clone.pending_update = False
        clone.pending_update_at = None
        clone.last_update_status = "synced"
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
        3. Get pending update requests to determine what actions to apply
        4. Sync workspace files (standard update)
        5. Apply optional actions (copy_files_folder, rebuild_environment)
        6. Update last_sync_at
        7. Clear pending_update flag
        8. Mark all pending update requests as applied

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

        # Get pending update requests to determine actions
        stmt = select(CloneUpdateRequest).where(
            CloneUpdateRequest.clone_agent_id == clone_id,
            CloneUpdateRequest.status == UpdateRequestStatus.PENDING
        )
        pending_requests = list(session.exec(stmt).all())

        # Determine what actions to apply (merge all pending requests)
        should_copy_files = any(req.copy_files_folder for req in pending_requests)
        should_rebuild_env = any(req.rebuild_environment for req in pending_requests)

        # Sync workspace files (standard update always happens)
        # include_files_folder is True only if any request has copy_files_folder=True
        if parent.active_environment_id and clone.active_environment_id:
            await AgentCloneService.copy_workspace(
                original_env_id=parent.active_environment_id,
                clone_env_id=clone.active_environment_id,
                include_files_folder=should_copy_files
            )
            logger.info(f"Copied workspace to clone {clone_id} (include_files={should_copy_files})")

        # Handle rebuild environment action
        if should_rebuild_env and clone.active_environment_id:
            logger.info(f"Triggering environment rebuild for clone {clone_id}")
            try:
                await EnvironmentService.rebuild_environment(
                    session=session,
                    env_id=clone.active_environment_id
                )
                logger.info(f"Environment rebuild completed for clone {clone_id}")
            except Exception as e:
                logger.error(f"Failed to rebuild environment for clone {clone_id}: {e}")

        # Update clone record
        clone.last_sync_at = datetime.now(UTC)
        clone.pending_update = False
        clone.pending_update_at = None
        clone.last_update_status = "synced"
        session.add(clone)

        # Mark all pending update requests as applied
        for req in pending_requests:
            req.status = UpdateRequestStatus.APPLIED
            req.applied_at = datetime.now(UTC)
            session.add(req)

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

    # ============ UPDATE REQUEST MANAGEMENT ============

    @staticmethod
    async def get_pending_update_requests(
        session: Session,
        clone_id: UUID,
        clone_owner_id: UUID
    ) -> list[CloneUpdateRequest]:
        """
        Get pending update requests for a clone.

        Args:
            session: Database session
            clone_id: The clone agent ID
            clone_owner_id: The owner of the clone

        Returns:
            List of pending CloneUpdateRequest records
        """
        clone = session.get(Agent, clone_id)
        if not clone:
            raise HTTPException(status_code=404, detail="Agent not found")
        if clone.owner_id != clone_owner_id:
            raise HTTPException(status_code=403, detail="Not your agent")
        if not clone.is_clone:
            return []

        stmt = select(CloneUpdateRequest).where(
            CloneUpdateRequest.clone_agent_id == clone_id,
            CloneUpdateRequest.status == UpdateRequestStatus.PENDING
        ).order_by(CloneUpdateRequest.created_at.desc())

        return list(session.exec(stmt).all())

    @staticmethod
    async def dismiss_update_request(
        session: Session,
        request_id: UUID,
        clone_owner_id: UUID
    ) -> CloneUpdateRequest:
        """
        Dismiss an update request.

        Args:
            session: Database session
            request_id: The update request ID
            clone_owner_id: The owner of the clone

        Returns:
            The dismissed CloneUpdateRequest
        """
        update_request = session.get(CloneUpdateRequest, request_id)
        if not update_request:
            raise HTTPException(status_code=404, detail="Update request not found")

        # Verify the clone belongs to the user
        clone = session.get(Agent, update_request.clone_agent_id)
        if not clone or clone.owner_id != clone_owner_id:
            raise HTTPException(status_code=403, detail="Not your agent")

        if update_request.status != UpdateRequestStatus.PENDING:
            raise HTTPException(status_code=400, detail="Update request is not pending")

        update_request.status = UpdateRequestStatus.DISMISSED
        update_request.dismissed_at = datetime.now(UTC)
        session.add(update_request)

        # Check if there are other pending requests for this clone
        stmt = select(CloneUpdateRequest).where(
            CloneUpdateRequest.clone_agent_id == update_request.clone_agent_id,
            CloneUpdateRequest.status == UpdateRequestStatus.PENDING,
            CloneUpdateRequest.id != request_id
        )
        other_pending = session.exec(stmt).first()

        # If no other pending requests, clear the clone's pending_update flag and set status to dismissed
        if not other_pending and clone:
            clone.pending_update = False
            clone.pending_update_at = None
            clone.last_update_status = "dismissed"
            session.add(clone)

        session.commit()
        session.refresh(update_request)

        logger.info(f"Dismissed update request {request_id} for clone {update_request.clone_agent_id}")
        return update_request

    @staticmethod
    async def check_and_apply_automatic_updates(
        session: Session,
        agent: Agent
    ) -> bool:
        """
        Check if agent is a clone with automatic updates pending and apply them.

        This method is called before environment suspension to apply any pending
        automatic updates while the environment is inactive.

        Args:
            session: Database session
            agent: The agent to check

        Returns:
            True if updates were applied, False otherwise
        """
        # Check if this is a clone with automatic mode and pending updates
        if not agent.is_clone:
            return False
        if agent.update_mode != "automatic":
            return False
        if not agent.pending_update:
            return False

        logger.info(f"Applying automatic update for clone {agent.id}")

        try:
            await AgentCloneService.apply_update(
                session=session,
                clone_id=agent.id,
                clone_owner_id=agent.owner_id
            )
            logger.info(f"Automatic update applied for clone {agent.id}")
            return True
        except Exception as e:
            logger.error(f"Failed to apply automatic update for clone {agent.id}: {e}", exc_info=True)
            return False
