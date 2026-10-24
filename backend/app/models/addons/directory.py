"""Wire shapes of ``GET /addons/directory`` (no tables).

One normalised row per addon a caller may add, whatever it comes from: a
catalog skill, a marketplace entry (skill or plugin) or an MCP directory
entry. Cinna Desktop builds its Add picker from this list and computes
compatibility itself from ``components`` — core does not know which engines a
given computer runs, so compatibility is deliberately not a field here.

The field set is a frozen contract shared with Cinna Desktop: renaming,
retyping or adding a field is a contract change, not a refactor.
"""
from typing import Literal, Optional, Union

from pydantic import ConfigDict
from sqlmodel import Field, SQLModel

from app.models.plugins.llm_plugin import PluginComponents
from app.models.skills.schemas import SkillCredentialRequirementPublic

#: Where an entry comes from; ``ref.id`` is opaque and stable per source.
DirectorySource = Literal["core-catalog", "core-marketplace", "core-mcp-directory"]
DirectoryKind = Literal["skill", "plugin", "mcp"]
DirectoryStatus = Literal["ready", "pending", "unsupported"]


class DirectoryRef(SQLModel):
    source: DirectorySource
    id: str


class DirectoryPublisher(SQLModel):
    name: Optional[str] = None
    verified: bool = False


class DirectoryOrigin(SQLModel):
    label: str
    kind: Literal["marketplace", "catalog", "directory"]
    repository_url: Optional[str] = None
    homepage: Optional[str] = None


class DirectoryRequires(SQLModel):
    credentials: list[SkillCredentialRequirementPublic] = Field(default_factory=list)


class McpPackage(SQLModel):
    """How a local-process (stdio) MCP server is fetched and started."""

    registry: Literal["npm", "pypi", "oci"]
    name: str = Field(min_length=1, max_length=255)
    version: Optional[str] = Field(default=None, max_length=64)
    args: list[str] = Field(
        default_factory=list,
        description=(
            "Command-line arguments, shown to every account. Never put a secret "
            "here: secrets belong in env, which carries names only."
        ),
    )


class McpEnvVar(SQLModel):
    """One value an MCP server needs from the user. Never a value, only a name."""

    #: Extra keys are kept so the MCP directory service can refuse an item
    #: carrying ``value`` with ``invalid_entry`` (a pydantic ``extra=forbid``
    #: error would echo the secret back in its ``input``).
    model_config = ConfigDict(extra="allow")

    name: str = Field(max_length=128)
    secret: bool = False
    description: Optional[str] = Field(default=None, max_length=500)


class McpConnectDescriptor(SQLModel):
    """What the desktop needs to connect an MCP directory entry."""

    transport: Literal["stdio", "http", "sse"]
    url: Optional[str] = None
    package: Optional[McpPackage] = None
    auth: Literal["none", "oauth", "bearer", "env"]
    env: list[McpEnvVar] = Field(default_factory=list)


class DirectoryArchiveInstall(SQLModel):
    archive_path: str


class DirectoryMcpInstall(SQLModel):
    mcp: McpConnectDescriptor


class DirectoryEntry(SQLModel):
    ref: DirectoryRef
    kind: DirectoryKind
    name: str
    title: str
    summary: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    icon_url: Optional[str] = None
    publisher: DirectoryPublisher
    origin: DirectoryOrigin
    version: Optional[str] = None
    digest: Optional[str] = None
    components: Optional[PluginComponents] = None
    requires: DirectoryRequires = Field(default_factory=DirectoryRequires)
    runs_code: bool = False
    status: DirectoryStatus
    unsupported_reason: Optional[str] = None
    install: Optional[Union[DirectoryArchiveInstall, DirectoryMcpInstall]] = None


class AddonsPolicy(SQLModel):
    """What this caller's desktop may do beyond the core directory.

    Every flag defaults to the behaviour that shipped before it existed
    (everything allowed), so an admin control can only tighten.
    """

    external_directories: bool = True
    custom_mcp: bool = True
    plugin_hooks: bool = True
    stdio_mcp: bool = True


class AddonsDirectoryPublic(SQLModel):
    data: list[DirectoryEntry]
    count: int
    policy: AddonsPolicy
    etag: str
