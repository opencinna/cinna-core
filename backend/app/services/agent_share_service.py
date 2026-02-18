"""
Agent Share Service - Handles agent sharing between users.

This service manages the sharing workflow:
- Creating share invitations
- Accepting/declining shares
- Revoking shares (with delete or detach options)
- Querying shares and clones
"""
from datetime import datetime
from uuid import UUID
from sqlmodel import Session, select
from fastapi import HTTPException

from app.models.agent import Agent
from app.models.agent_share import AgentShare, ShareMode, ShareSource, ShareStatus
from app.models.ai_credential import AICredential
from app.models.user import User


class AgentShareService:
    """Service for managing agent sharing operations."""

    @staticmethod
    async def share_agent(
        session: Session,
        agent_id: UUID,
        owner_id: UUID,
        shared_with_email: str,
        share_mode: str,
        provide_ai_credentials: bool = False,
        conversation_ai_credential_id: UUID | None = None,
        building_ai_credential_id: UUID | None = None
    ) -> AgentShare:
        """
        Initiate sharing an agent with another user.
        Creates pending share record.

        Validations:
        1. Agent must exist and be owned by owner_id
        2. Agent must not be a clone (clones cannot be re-shared)
        3. share_mode must be valid ("user" or "builder")
        4. Target user must exist (by email)
        5. Cannot share with yourself
        6. Cannot create duplicate share (unique constraint)
        7. If providing AI credentials, they must be owned by owner

        Returns: AgentShare with status="pending"
        """
        # 1. Get and validate agent
        agent = session.get(Agent, agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        if agent.owner_id != owner_id:
            raise HTTPException(status_code=403, detail="Not your agent")

        # 2. Clones cannot be shared
        if agent.is_clone:
            raise HTTPException(
                status_code=400,
                detail="Cannot share a cloned agent. Detach it first to enable sharing."
            )

        # 3. Validate share_mode
        if share_mode not in [ShareMode.USER, ShareMode.BUILDER]:
            raise HTTPException(status_code=400, detail="Invalid share mode")

        # 4. Find target user by email
        stmt = select(User).where(User.email == shared_with_email)
        target_user = session.exec(stmt).first()
        if not target_user:
            raise HTTPException(
                status_code=404,
                detail="User not found. They must sign up first."
            )

        # 5. Cannot share with yourself
        if target_user.id == owner_id:
            raise HTTPException(status_code=400, detail="Cannot share with yourself")

        # 7. Validate AI credentials if provided
        if provide_ai_credentials:
            if conversation_ai_credential_id:
                conv_cred = session.get(AICredential, conversation_ai_credential_id)
                if not conv_cred or conv_cred.owner_id != owner_id:
                    raise HTTPException(
                        status_code=400,
                        detail="Conversation AI credential not found or not owned by you"
                    )
            if building_ai_credential_id:
                build_cred = session.get(AICredential, building_ai_credential_id)
                if not build_cred or build_cred.owner_id != owner_id:
                    raise HTTPException(
                        status_code=400,
                        detail="Building AI credential not found or not owned by you"
                    )

        # 6. Check for existing share
        stmt = select(AgentShare).where(
            AgentShare.original_agent_id == agent_id,
            AgentShare.shared_with_user_id == target_user.id
        )
        existing = session.exec(stmt).first()
        if existing:
            if existing.status == ShareStatus.PENDING:
                raise HTTPException(status_code=400, detail="Share already pending")
            elif existing.status == ShareStatus.ACCEPTED:
                raise HTTPException(status_code=400, detail="Already shared with this user")
            elif existing.status in [ShareStatus.DECLINED, ShareStatus.REVOKED]:
                # Allow re-sharing after decline/revoke - update existing record
                existing.status = ShareStatus.PENDING
                existing.share_mode = share_mode
                existing.shared_at = datetime.utcnow()
                existing.accepted_at = None
                existing.declined_at = None
                existing.cloned_agent_id = None
                existing.provide_ai_credentials = provide_ai_credentials
                existing.conversation_ai_credential_id = conversation_ai_credential_id if provide_ai_credentials else None
                existing.building_ai_credential_id = building_ai_credential_id if provide_ai_credentials else None
                session.add(existing)
                session.commit()
                session.refresh(existing)
                return existing

        # Create new share record
        share = AgentShare(
            original_agent_id=agent_id,
            shared_with_user_id=target_user.id,
            shared_by_user_id=owner_id,
            share_mode=share_mode,
            status=ShareStatus.PENDING,
            shared_at=datetime.utcnow(),
            provide_ai_credentials=provide_ai_credentials,
            conversation_ai_credential_id=conversation_ai_credential_id if provide_ai_credentials else None,
            building_ai_credential_id=building_ai_credential_id if provide_ai_credentials else None
        )
        session.add(share)
        session.commit()
        session.refresh(share)
        return share

    @staticmethod
    async def create_auto_share(
        session: Session,
        agent_id: UUID,
        user_id: UUID,
        share_mode: str,
        source: str = ShareSource.EMAIL_INTEGRATION,
    ) -> tuple[AgentShare, "Agent"]:
        """
        Create a pre-accepted share and clone for email integration.

        - Creates share with status='accepted' directly (no pending state)
        - Calls AgentCloneService.create_clone() immediately
        - Returns (share, clone_agent)

        Raises ValueError if agent not found or is a clone.
        """
        from app.services.agent_clone_service import AgentCloneService

        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError("Agent not found")
        if agent.is_clone:
            raise ValueError("Cannot create auto-share on a clone agent")

        # Check for existing share for this user
        stmt = select(AgentShare).where(
            AgentShare.original_agent_id == agent_id,
            AgentShare.shared_with_user_id == user_id,
        )
        existing = session.exec(stmt).first()
        if existing and existing.status == ShareStatus.ACCEPTED:
            # Already has a share+clone - return existing
            if existing.cloned_agent_id:
                clone = session.get(Agent, existing.cloned_agent_id)
                if clone:
                    return existing, clone

        # Create share record (pre-accepted)
        share = AgentShare(
            original_agent_id=agent_id,
            shared_with_user_id=user_id,
            shared_by_user_id=agent.owner_id,
            share_mode=share_mode,
            status=ShareStatus.ACCEPTED,
            source=source,
            shared_at=datetime.utcnow(),
            accepted_at=datetime.utcnow(),
        )
        session.add(share)
        session.commit()
        session.refresh(share)

        # Create clone
        clone = await AgentCloneService.create_clone(
            session=session,
            original_agent=agent,
            recipient_id=user_id,
            clone_mode=share_mode,
            credentials_data={},
            share=share,
        )

        # Update share with clone reference
        share.cloned_agent_id = clone.id
        session.add(share)
        session.commit()

        return share, clone

    @staticmethod
    async def accept_share(
        session: Session,
        share_id: UUID,
        recipient_id: UUID,
        credentials_data: dict | None = None,
        ai_credential_selections: dict | None = None
    ) -> Agent:
        """
        Accept a pending share and create the clone.

        Validations:
        1. Share must exist
        2. Share must be pending
        3. Recipient must match share target

        Process:
        1. Create cloned agent via AgentCloneService
        2. Update share record status
        3. Return created clone

        Args:
            session: Database session
            share_id: Share ID
            recipient_id: Recipient user ID
            credentials_data: Optional dict of {credential_id: {field: value}} for placeholders
            ai_credential_selections: Optional dict with conversation_credential_id/building_credential_id

        Returns: Created Agent (the clone)
        """
        # Import here to avoid circular imports
        from app.services.agent_clone_service import AgentCloneService

        # 1. Get and validate share
        share = session.get(AgentShare, share_id)
        if not share:
            raise HTTPException(status_code=404, detail="Share not found")

        # 2. Must be pending
        if share.status != ShareStatus.PENDING:
            raise HTTPException(
                status_code=400,
                detail=f"Share is not pending (status: {share.status})"
            )

        # 3. Recipient must match
        if share.shared_with_user_id != recipient_id:
            raise HTTPException(status_code=403, detail="This share is not for you")

        # Get original agent
        original_agent = session.get(Agent, share.original_agent_id)
        if not original_agent:
            raise HTTPException(status_code=404, detail="Original agent no longer exists")

        # Create the clone with share info
        clone = await AgentCloneService.create_clone(
            session=session,
            original_agent=original_agent,
            recipient_id=recipient_id,
            clone_mode=share.share_mode,
            credentials_data=credentials_data or {},
            share=share,
            ai_credential_selections=ai_credential_selections
        )

        # Update share record
        share.status = ShareStatus.ACCEPTED
        share.accepted_at = datetime.utcnow()
        share.cloned_agent_id = clone.id
        session.add(share)
        session.commit()

        return clone

    @staticmethod
    async def decline_share(
        session: Session,
        share_id: UUID,
        recipient_id: UUID
    ) -> None:
        """
        Decline a pending share.

        Validations:
        1. Share must exist
        2. Share must be pending
        3. Recipient must match
        """
        share = session.get(AgentShare, share_id)
        if not share:
            raise HTTPException(status_code=404, detail="Share not found")

        if share.status != ShareStatus.PENDING:
            raise HTTPException(status_code=400, detail="Share is not pending")

        if share.shared_with_user_id != recipient_id:
            raise HTTPException(status_code=403, detail="This share is not for you")

        share.status = ShareStatus.DECLINED
        share.declined_at = datetime.utcnow()
        session.add(share)
        session.commit()

    @staticmethod
    async def revoke_share(
        session: Session,
        share_id: UUID,
        owner_id: UUID,
        action: str  # "delete" or "detach"
    ) -> None:
        """
        Owner revokes an existing share.

        action="delete":
        - Clone is DELETED with all its data
        - Use when: access should be completely removed

        action="detach":
        - Clone becomes independent (parent_agent_id = null)
        - Clone owner keeps functional agent with all data
        - Use when: user should keep their work

        Validations:
        1. Share must exist
        2. Original agent must be owned by owner_id
        3. action must be "delete" or "detach"
        """
        share = session.get(AgentShare, share_id)
        if not share:
            raise HTTPException(status_code=404, detail="Share not found")

        # Verify ownership
        original_agent = session.get(Agent, share.original_agent_id)
        if not original_agent or original_agent.owner_id != owner_id:
            raise HTTPException(status_code=403, detail="Not your agent")

        if action not in ["delete", "detach"]:
            raise HTTPException(status_code=400, detail="Invalid action")

        # Handle the clone if it exists
        if share.cloned_agent_id:
            clone = session.get(Agent, share.cloned_agent_id)
            if clone:
                if action == "delete":
                    # Delete the clone using AgentService to properly cleanup resources
                    from app.services.agent_service import AgentService
                    await AgentService.delete_agent(session=session, agent_id=clone.id)
                elif action == "detach":
                    # Detach clone from parent
                    clone.is_clone = False
                    clone.parent_agent_id = None
                    clone.clone_mode = None
                    clone.pending_update = False
                    clone.pending_update_at = None
                    session.add(clone)

        # Update share status
        share.status = ShareStatus.REVOKED
        share.cloned_agent_id = None  # Clear reference
        session.add(share)
        session.commit()

    @staticmethod
    async def get_pending_shares(
        session: Session,
        user_id: UUID
    ) -> list[AgentShare]:
        """
        List all pending shares for a user (as recipient).
        """
        stmt = select(AgentShare).where(
            AgentShare.shared_with_user_id == user_id,
            AgentShare.status == ShareStatus.PENDING
        ).order_by(AgentShare.shared_at.desc())
        return list(session.exec(stmt).all())

    @staticmethod
    async def get_agent_shares(
        session: Session,
        agent_id: UUID,
        owner_id: UUID
    ) -> list[AgentShare]:
        """
        List all shares for an agent (for owner).
        """
        # Verify ownership
        agent = session.get(Agent, agent_id)
        if not agent or agent.owner_id != owner_id:
            raise HTTPException(status_code=403, detail="Not your agent")

        stmt = select(AgentShare).where(
            AgentShare.original_agent_id == agent_id
        ).order_by(AgentShare.shared_at.desc())
        return list(session.exec(stmt).all())

    @staticmethod
    async def get_agent_clones(
        session: Session,
        agent_id: UUID,
        owner_id: UUID
    ) -> list[Agent]:
        """
        List all clones of an agent (for owner).
        """
        agent = session.get(Agent, agent_id)
        if not agent or agent.owner_id != owner_id:
            raise HTTPException(status_code=403, detail="Not your agent")

        stmt = select(Agent).where(Agent.parent_agent_id == agent_id)
        return list(session.exec(stmt).all())

    @staticmethod
    async def get_share_by_id(
        session: Session,
        share_id: UUID
    ) -> AgentShare | None:
        """Get a share by ID."""
        return session.get(AgentShare, share_id)

    @staticmethod
    async def delete_share_record(
        session: Session,
        share_id: UUID,
        owner_id: UUID
    ) -> None:
        """
        Permanently delete a share record from the database.

        Only allowed for shares in terminal states: deleted, declined, revoked.
        Used for cleanup when the share record is no longer needed.

        Validations:
        1. Share must exist
        2. Original agent must be owned by owner_id
        3. Share must be in a terminal state
        """
        share = session.get(AgentShare, share_id)
        if not share:
            raise HTTPException(status_code=404, detail="Share not found")

        # Verify ownership
        original_agent = session.get(Agent, share.original_agent_id)
        if not original_agent or original_agent.owner_id != owner_id:
            raise HTTPException(status_code=403, detail="Not your agent")

        # Only allow deletion of terminal state shares
        terminal_states = [ShareStatus.DELETED, ShareStatus.DECLINED, ShareStatus.REVOKED]
        if share.status not in terminal_states:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot delete share in '{share.status}' state. Use revoke instead."
            )

        session.delete(share)
        session.commit()
