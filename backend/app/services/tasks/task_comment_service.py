"""
Task Comment Service — manages comments on input tasks.

Comments are the primary collaboration surface: agents and users post findings,
results, and progress updates as comments. System events (status changes,
assignments, subtask notifications) also appear as system-type comments.
"""
import logging
from uuid import UUID

from sqlmodel import Session as DBSession, select

from app.models.tasks.task_comment import TaskComment, TaskCommentCreate, AgentTaskCommentCreate, TaskCommentPublic
from app.models.tasks.task_attachment import TaskAttachmentPublic
from app.models.tasks.input_task import InputTask
from app.models.agents.agent import Agent
from app.models.events.event import EventType
from app.services.events.event_service import event_service
from app.utils import create_task_with_error_logging

logger = logging.getLogger(__name__)


class TaskCommentService:
    """Service for managing task comments."""

    @staticmethod
    def _resolve_author_name(
        db_session: DBSession,
        author_agent_id: UUID | None,
        author_user_id: UUID | None,
    ) -> str | None:
        """Resolve author display name from agent or user ID."""
        if author_agent_id:
            from app.models.agents.agent import Agent
            agent = db_session.get(Agent, author_agent_id)
            if agent:
                return agent.name
        if author_user_id:
            from app.models.users.user import User
            user = db_session.get(User, author_user_id)
            if user:
                return user.full_name or user.email
        return None

    @staticmethod
    def _resolve_author_role(
        db_session: DBSession,
        author_node_id: UUID | None,
    ) -> str | None:
        """Resolve team node name for team-context comments."""
        if not author_node_id:
            return None
        from app.models.agentic_teams.agentic_team import AgenticTeamNode
        node = db_session.get(AgenticTeamNode, author_node_id)
        if node:
            return node.name
        return None

    @staticmethod
    def _to_public(
        db_session: DBSession,
        comment: TaskComment,
        include_attachments: bool = True,
    ) -> TaskCommentPublic:
        """Convert a TaskComment DB object to its public schema."""
        author_name = TaskCommentService._resolve_author_name(
            db_session,
            comment.author_agent_id,
            comment.author_user_id,
        )
        author_role = TaskCommentService._resolve_author_role(
            db_session,
            comment.author_node_id,
        )

        inline_attachments: list[TaskAttachmentPublic] = []
        if include_attachments:
            from app.models.tasks.task_attachment import TaskAttachment
            attachments = db_session.exec(
                select(TaskAttachment).where(TaskAttachment.comment_id == comment.id)
            ).all()
            for att in attachments:
                inline_attachments.append(
                    TaskCommentService._attachment_to_public(db_session, att)
                )

        return TaskCommentPublic(
            id=comment.id,
            task_id=comment.task_id,
            content=comment.content,
            comment_type=comment.comment_type,
            author_node_id=comment.author_node_id,
            author_agent_id=comment.author_agent_id,
            author_user_id=comment.author_user_id,
            comment_meta=comment.comment_meta,
            created_at=comment.created_at,
            author_name=author_name,
            author_role=author_role,
            inline_attachments=inline_attachments,
        )

    @staticmethod
    def _attachment_to_public(
        db_session: DBSession,
        attachment,
    ) -> TaskAttachmentPublic:
        """Convert a TaskAttachment to its public schema."""
        uploaded_by_name = None
        if attachment.uploaded_by_agent_id:
            agent = db_session.get(Agent, attachment.uploaded_by_agent_id)
            if agent:
                uploaded_by_name = agent.name
        elif attachment.uploaded_by_user_id:
            from app.models.users.user import User
            user = db_session.get(User, attachment.uploaded_by_user_id)
            if user:
                uploaded_by_name = user.full_name or user.email

        source_agent_name = None
        if attachment.source_agent_id:
            agent = db_session.get(Agent, attachment.source_agent_id)
            if agent:
                source_agent_name = agent.name

        download_url = f"/api/v1/tasks/{attachment.task_id}/attachments/{attachment.id}/download"

        return TaskAttachmentPublic(
            id=attachment.id,
            task_id=attachment.task_id,
            comment_id=attachment.comment_id,
            file_name=attachment.file_name,
            file_path=attachment.file_path,
            file_size=attachment.file_size,
            content_type=attachment.content_type,
            uploaded_by_agent_id=attachment.uploaded_by_agent_id,
            uploaded_by_user_id=attachment.uploaded_by_user_id,
            source_agent_id=attachment.source_agent_id,
            source_workspace_path=attachment.source_workspace_path,
            created_at=attachment.created_at,
            uploaded_by_name=uploaded_by_name,
            source_agent_name=source_agent_name,
            download_url=download_url,
        )

    @staticmethod
    def add_comment(
        db_session: DBSession,
        task_id: UUID,
        data: TaskCommentCreate,
        author_agent_id: UUID | None = None,
        author_node_id: UUID | None = None,
        author_user_id: UUID | None = None,
    ) -> TaskComment:
        """
        Create a comment on a task.

        Args:
            db_session: Database session
            task_id: Target task UUID
            data: Comment content and type
            author_agent_id: Agent posting the comment (or None for system/user)
            author_node_id: Team node if agent is in team context
            author_user_id: User posting the comment

        Returns:
            Created TaskComment
        """
        comment = TaskComment(
            task_id=task_id,
            content=data.content,
            comment_type=data.comment_type,
            author_agent_id=author_agent_id,
            author_node_id=author_node_id,
            author_user_id=author_user_id,
        )
        db_session.add(comment)
        db_session.commit()
        db_session.refresh(comment)

        # Resolve task owner for targeted event delivery
        task = db_session.get(InputTask, task_id)
        if task:
            author_name = TaskCommentService._resolve_author_name(
                db_session, author_agent_id, author_user_id
            )
            create_task_with_error_logging(
                event_service.emit_event(
                    event_type=EventType.TASK_COMMENT_ADDED,
                    model_id=task_id,
                    user_id=task.owner_id,
                    meta={
                        "task_id": str(task_id),
                        "short_code": task.short_code,
                        "comment_id": str(comment.id),
                        "author_name": author_name,
                        "has_attachments": False,
                    }
                ),
                task_name=f"emit_task_comment_added_{comment.id}"
            )

        logger.info(f"Comment {comment.id} added to task {task_id}")
        return comment

    @staticmethod
    def add_comment_from_agent(
        db_session: DBSession,
        task_id: UUID,
        agent_id: UUID,
        data: AgentTaskCommentCreate,
    ) -> TaskComment:
        """
        Agent posts a comment on a task (potentially with file attachments).

        Resolves the agent's team node if the task has team context, then creates
        the comment. If file_paths are provided, delegates to TaskAttachmentService
        to fetch files from the agent workspace and link them.

        Args:
            db_session: Database session
            task_id: Target task UUID
            agent_id: Agent posting the comment
            data: Comment content, type, and optional workspace file paths

        Returns:
            Created TaskComment
        """
        # Determine team context: find the agent's node in the task's team (if any)
        author_node_id: UUID | None = None
        task = db_session.get(InputTask, task_id)
        if task and task.team_id:
            from app.models.agentic_teams.agentic_team import AgenticTeamNode
            node = db_session.exec(
                select(AgenticTeamNode).where(
                    AgenticTeamNode.team_id == task.team_id,
                    AgenticTeamNode.agent_id == agent_id,
                )
            ).first()
            if node:
                author_node_id = node.id

        # Create the comment
        comment_data = TaskCommentCreate(
            content=data.content,
            comment_type=data.comment_type,
        )
        comment = TaskCommentService.add_comment(
            db_session=db_session,
            task_id=task_id,
            data=comment_data,
            author_agent_id=agent_id,
            author_node_id=author_node_id,
        )

        # Attach workspace files if provided
        if data.file_paths:
            try:
                from app.services.tasks.task_attachment_service import TaskAttachmentService
                TaskAttachmentService.attach_from_workspace(
                    db_session=db_session,
                    task_id=task_id,
                    agent_id=agent_id,
                    file_paths=data.file_paths,
                    comment_id=comment.id,
                )
            except Exception as e:
                logger.warning(
                    f"Failed to attach files for comment {comment.id}: {e}",
                    exc_info=True,
                )

        return comment

    @staticmethod
    def add_system_comment(
        db_session: DBSession,
        task_id: UUID,
        content: str,
        comment_type: str = "system",
        comment_meta: dict | None = None,
    ) -> TaskComment:
        """
        Create a system-generated comment (no author fields set).

        Used by the service layer for status changes, assignment changes,
        and subtask notifications.

        Args:
            db_session: Database session
            task_id: Target task UUID
            content: System message text
            comment_type: Type identifier (system, status_change, assignment)
            comment_meta: Optional metadata dict

        Returns:
            Created TaskComment
        """
        comment = TaskComment(
            task_id=task_id,
            content=content,
            comment_type=comment_type,
            comment_meta=comment_meta,
        )
        db_session.add(comment)
        db_session.commit()
        db_session.refresh(comment)

        logger.debug(f"System comment added to task {task_id}: type={comment_type}")
        return comment

    @staticmethod
    def list_comments(
        db_session: DBSession,
        task_id: UUID,
        skip: int = 0,
        limit: int = 100,
    ) -> tuple[list[TaskCommentPublic], int]:
        """
        List comments for a task in chronological order (oldest first).

        Args:
            db_session: Database session
            task_id: Task UUID
            skip: Pagination offset
            limit: Max records to return

        Returns:
            Tuple of (list of public comments, total count)
        """
        base_stmt = select(TaskComment).where(TaskComment.task_id == task_id)
        total = len(db_session.exec(base_stmt).all())

        comments = db_session.exec(
            base_stmt
            .order_by(TaskComment.created_at.asc())
            .offset(skip)
            .limit(limit)
        ).all()

        return [
            TaskCommentService._to_public(db_session, c)
            for c in comments
        ], total

    @staticmethod
    def delete_comment(
        db_session: DBSession,
        comment_id: UUID,
        user_id: UUID,
    ) -> bool:
        """
        Delete a comment if the user owns the task it belongs to.

        Args:
            db_session: Database session
            comment_id: Comment UUID to delete
            user_id: Requesting user (ownership check via task)

        Returns:
            True if deleted, False if not found or unauthorized
        """
        comment = db_session.get(TaskComment, comment_id)
        if not comment:
            return False

        task = db_session.get(InputTask, comment.task_id)
        if not task or task.owner_id != user_id:
            return False

        db_session.delete(comment)
        db_session.commit()
        return True
