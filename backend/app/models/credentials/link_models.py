import uuid
from sqlmodel import Field, SQLModel


# Many-to-many link table for agents and credentials
class AgentCredentialLink(SQLModel, table=True):
    __tablename__ = "agent_credential_link"
    agent_id: uuid.UUID = Field(
        foreign_key="agent.id", primary_key=True, ondelete="CASCADE"
    )
    credential_id: uuid.UUID = Field(
        foreign_key="credential.id", primary_key=True, ondelete="CASCADE"
    )
