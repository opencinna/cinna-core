"""Wire models for publishing an artifact out of an agent environment's workspace.

Two callers, one service (``ArtifactSourceService``):

* the agent's ``artifacts_publish`` tool → ``POST /agent/artifacts/publish``
  (env token); field names and limits follow
  ``docs/local_agent_kit/protocols/artifacts.schema.json``;
* the owner's "Publish as artifact" in the workspace file view →
  ``POST /artifacts/from-environment`` (user JWT, D20).

Both request models forbid extra fields: an agent has no visibility, grant,
link or delete control (A8), and a stray ``visibility`` is a 422, not ignored.
"""
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.artifacts.artifact import ArtifactKindLiteral


class _WorkspacePublish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A file or folder relative to the workspace root.
    path: str = Field(min_length=1, max_length=1024)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    # The HTML start page, relative to the folder.
    entry: str | None = Field(default=None, max_length=1024)
    # Republish into this artifact; omitted ⇒ match by path (A12) or create.
    artifact_id: uuid.UUID | None = None


class AgentArtifactPublishRequest(_WorkspacePublish):
    # The calling conversation, when the tool has one (recorded on a new artifact).
    source_session_id: uuid.UUID | None = None


class ArtifactFromEnvironmentRequest(_WorkspacePublish):
    environment_id: uuid.UUID


class AgentArtifactPublished(BaseModel):
    """The ``artifacts_publish`` result, exactly as the schema declares it."""

    artifact_id: uuid.UUID
    revision: int
    # The artifact's core page (needs the owner's sign-in), not a content URL.
    url: str
    kind: ArtifactKindLiteral
    file_count: int
    # Bytes in this revision.
    size_bytes: int
    excluded: list[str]
