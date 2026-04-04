from uuid import UUID
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

from app.models.agentic_teams.agentic_team import (
    AgenticTeam,
    AgenticTeamCreate,
    AgenticTeamUpdate,
)


class AgenticTeamService:

    @staticmethod
    def get_team(session: Session, team_id: UUID, user_id: UUID) -> AgenticTeam:
        """
        Fetch team by ID. Raises 404 if the team does not exist OR if the
        requesting user is not the owner. Returns the same 404 in both cases
        to avoid leaking information about other users' teams.
        """
        team = session.get(AgenticTeam, team_id)
        if not team or team.owner_id != user_id:
            raise HTTPException(status_code=404, detail="Agentic team not found")
        return team

    @staticmethod
    def list_teams(
        session: Session, user_id: UUID, skip: int = 0, limit: int = 100
    ) -> tuple[list[AgenticTeam], int]:
        """Return (teams, total_count) for the given user."""
        statement = (
            select(AgenticTeam)
            .where(AgenticTeam.owner_id == user_id)
            .offset(skip)
            .limit(limit)
        )
        teams = list(session.exec(statement).all())

        count_statement = select(func.count()).select_from(AgenticTeam).where(
            AgenticTeam.owner_id == user_id
        )
        count = session.exec(count_statement).one()

        return teams, count

    @staticmethod
    def create_team(
        session: Session, user_id: UUID, data: AgenticTeamCreate
    ) -> AgenticTeam:
        team = AgenticTeam.model_validate(data, update={"owner_id": user_id})
        session.add(team)
        session.commit()
        session.refresh(team)
        return team

    @staticmethod
    def update_team(
        session: Session, team_id: UUID, user_id: UUID, data: AgenticTeamUpdate
    ) -> AgenticTeam:
        team = AgenticTeamService.get_team(session, team_id, user_id)
        update_dict = data.model_dump(exclude_unset=True)
        team.sqlmodel_update(update_dict)
        team.updated_at = datetime.now(timezone.utc)
        session.add(team)
        session.commit()
        session.refresh(team)
        return team

    @staticmethod
    def delete_team(session: Session, team_id: UUID, user_id: UUID) -> None:
        team = AgenticTeamService.get_team(session, team_id, user_id)
        session.delete(team)
        session.commit()
