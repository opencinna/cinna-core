from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Any


class HealthCheckResponse(BaseModel):
    """Health check response model"""
    status: str  # "healthy" | "degraded" | "unhealthy"
    timestamp: datetime
    uptime: int  # Seconds since startup
    message: str | None = None


class AgentPromptsResponse(BaseModel):
    """Agent prompts from docs files"""
    workflow_prompt: str | None = None
    entrypoint_prompt: str | None = None


class AgentPromptsUpdate(BaseModel):
    """Update agent prompts in docs files"""
    workflow_prompt: str | None = None
    entrypoint_prompt: str | None = None


class ChatRequest(BaseModel):
    """Chat message request"""
    message: str
    session_id: Optional[str] = None  # External SDK session ID (for Claude SDK resumption)
    backend_session_id: Optional[str] = None  # Backend database session ID (for handover tracking)
    mode: str = "conversation"  # "building" | "conversation"
    agent_sdk: str = "claude"  # SDK to use: "claude" (more options can be added later)
    system_prompt: Optional[str] = None


class ChatResponse(BaseModel):
    """Chat message response"""
    response: str
    session_id: Optional[str] = None
    metadata: dict = {}


class CredentialsUpdate(BaseModel):
    """Update credentials in workspace"""
    credentials_json: list[dict]  # Full credentials data
    credentials_readme: str  # Redacted README for agent prompt


class FileNode(BaseModel):
    """Single file or folder node in workspace tree"""
    name: str
    type: str  # "file" | "folder"
    path: str  # Relative path from workspace root
    size: int | None = None  # Bytes (None for folders until summarized)
    modified: datetime | None = None
    children: list['FileNode'] | None = None  # Only for folders


class FolderSummary(BaseModel):
    """Summary statistics for a folder"""
    fileCount: int
    totalSize: int  # Bytes


class WorkspaceTreeResponse(BaseModel):
    """Complete workspace tree with 5 main folders"""
    files: FileNode
    logs: FileNode
    scripts: FileNode
    docs: FileNode
    uploads: FileNode
    summaries: dict[str, FolderSummary]


class AgentHandoverUpdate(BaseModel):
    """Update agent handover configuration"""
    handovers: list[dict]  # Array of {id, name, prompt} objects
    handover_prompt: str  # Prompt text to append to conversation mode system prompt


class AgentHandoverResponse(BaseModel):
    """Agent handover configuration response"""
    handovers: list[dict]
    handover_prompt: str


class FileUploadResponse(BaseModel):
    """Response for file upload endpoint"""
    path: str  # Relative path: ./uploads/document.pdf
    filename: str  # Final filename (may differ from requested if conflict)
    size: int  # File size in bytes
    message: str


# SQLite Database Models

class DatabaseTableEntry(BaseModel):
    """Simple table/view entry with name and type"""
    name: str
    type: str  # "table" | "view"


class SQLiteColumnInfo(BaseModel):
    """Column information for a SQLite table/view"""
    name: str
    type: str  # SQLite type: TEXT, INTEGER, REAL, BLOB, NULL
    nullable: bool
    primary_key: bool


class SQLiteTableInfo(BaseModel):
    """Information about a table or view in SQLite database"""
    name: str
    type: str  # "table" | "view"
    columns: list[SQLiteColumnInfo]


class SQLiteDatabaseSchema(BaseModel):
    """Complete schema information for a SQLite database"""
    path: str  # Relative path to database file
    tables: list[SQLiteTableInfo]
    views: list[SQLiteTableInfo]


class SQLiteQueryRequest(BaseModel):
    """Request to execute SQL query on SQLite database"""
    path: str  # Relative path to SQLite file
    query: str  # SQL query to execute
    page: int | None = None  # Page number (1-based), None = no pagination
    page_size: int | None = None  # Rows per page, None = no pagination
    timeout_seconds: int = 30  # Query timeout


class SQLiteQueryResult(BaseModel):
    """Result from SQL query execution"""
    columns: list[str]  # Column names
    rows: list[list[Any]]  # Row data as list of lists
    total_rows: int  # Total row count (for SELECT queries)
    page: int | None  # Current page (None if no pagination)
    page_size: int | None  # Page size used (None if no pagination)
    has_more: bool  # Whether more pages exist
    execution_time_ms: float  # Query execution time
    query_type: str  # "SELECT" | "INSERT" | "UPDATE" | "DELETE" | "OTHER"
    rows_affected: int | None = None  # For DML queries
    error: str | None = None  # Error message if query failed
    error_type: str | None = None  # "syntax_error" | "timeout" | "file_error" | "execution_error"
