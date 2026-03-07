# Remote Database Viewer - Technical Details

## File Locations

### Agent-Env (inside Docker container)

- **Routes:** `backend/app/env-templates/app_core_base/core/server/routes.py` - `/database/tables/{path}`, `/database/schema/{path}`, `/database/query`
- **Service:** `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` - SQLite query execution, schema introspection
- **Models:** `backend/app/env-templates/app_core_base/core/server/models.py` - SQLite data models

### Backend

- **Routes:** `backend/app/api/routes/workspace.py` - Proxy endpoints for database operations
- **Adapter:** `backend/app/services/adapters/docker_adapter.py` - HTTP forwarding to agent-env

### Frontend

- **Route:** `frontend/src/routes/_layout/environment/$envId/database.tsx` - Database viewer page
- **Components:** `frontend/src/components/Environment/DatabaseViewer.tsx` - Main viewer component
- **Components:** `frontend/src/components/Environment/TreeItemRenderer.tsx` - SQLite file expansion in tree
- **Components:** `frontend/src/components/Environment/EnvironmentPanel.tsx` - Database table state management
- **Components:** `frontend/src/components/Environment/FileIcon.tsx` - Orange database icon
- **Types:** `frontend/src/components/Environment/types.ts` - DatabaseTableItem, SQLite type definitions
- **Utils:** `frontend/src/components/Environment/utils.ts` - `isSQLiteFile()`, `getFileExtension()`

## API Endpoints

### Agent-Env Endpoints - `backend/app/env-templates/app_core_base/core/server/routes.py`

- `GET /database/tables/{path}` - List table/view names (lightweight)
- `GET /database/schema/{path}` - Full schema with columns and types
- `POST /database/query` - Execute SQL query with pagination

### Backend Proxy Endpoints - `backend/app/api/routes/workspace.py`

- `GET /api/v1/environments/{env_id}/database/tables/{path}` - Proxy to agent-env tables endpoint
- `GET /api/v1/environments/{env_id}/database/schema/{path}` - Proxy to agent-env schema endpoint
- `POST /api/v1/environments/{env_id}/database/query` - Proxy to agent-env query endpoint

## Services & Key Methods

### Agent-Env Service - `backend/app/env-templates/app_core_base/core/server/agent_env_service.py`

- `is_sqlite_file(filename)` - Check if file has SQLite extension
- `get_database_tables(path)` - Return list of table/view names
- `get_database_schema(path)` - Return full schema with `PRAGMA table_info`
- `execute_query(path, query, page, page_size, timeout)` - Execute SQL with pagination

**Query execution logic:**
1. Path validation via `validate_workspace_path()` to prevent directory traversal
2. Query type detection: parses first keyword (SELECT, INSERT, UPDATE, DELETE, OTHER)
3. SELECT queries: wraps in COUNT subquery for total count, applies LIMIT/OFFSET, returns columns + rows + pagination
4. DML queries: executes and commits, returns `rows_affected`
5. Errors returned in response (not HTTP error) with classification

### DockerAdapter - `backend/app/services/adapters/docker_adapter.py`

- `get_database_tables(path)` - GET request to agent-env
- `get_database_schema(path)` - GET request to agent-env
- `execute_database_query(path, query, page, page_size, timeout)` - POST request to agent-env

### Backend Proxy Routes - `backend/app/api/routes/workspace.py`

- Standard permission checks (user owns agent)
- Environment must be running
- Forwards to adapter methods
- Query errors returned in response format (not HTTP errors)

## Data Models

### Agent-Env Models - `backend/app/env-templates/app_core_base/core/server/models.py`

- `SQLiteColumnInfo` - Column metadata: name, type, nullable, primary_key
- `SQLiteTableInfo` - Table/view with columns list
- `SQLiteDatabaseSchema` - Complete schema: path, tables, views
- `SQLiteQueryRequest` - Query parameters: path, query, page, page_size, timeout_seconds
- `SQLiteQueryResult` - Results: columns, rows, pagination info, execution stats, error handling

### Backend Models - `backend/app/api/routes/workspace.py`

- `DatabaseQueryRequest` - Pydantic model for query proxy endpoint

### Frontend Types - `frontend/src/components/Environment/types.ts`

- `DatabaseTableItem` - Tree item type: name, tableType, databasePath
- `SQLiteColumnInfo`, `SQLiteTableInfo`, `SQLiteDatabaseSchema`
- `SQLiteQueryRequest`, `SQLiteQueryResult`

## Frontend Components

### DatabaseViewer - `frontend/src/components/Environment/DatabaseViewer.tsx`

Main viewer with:
- Schema sidebar: expandable tables/views with column details
- Mode toggle: switch between Auto/Manual modes
- Query editor: textarea (read-only in Auto, editable in Manual)
- Results table: displays query results with sticky headers
- Status bar: row count, execution time, pagination controls
- CSV export: download current results as CSV file

**Key state:**
- `mode`: "auto" | "manual"
- `selectedTable`: currently selected table name
- `query`: SQL query text (Manual mode)
- `page`: current page number (Auto mode)
- `expandedSchema`: set of expanded schema sections

### Tree Integration

**TreeItemRenderer** - `frontend/src/components/Environment/TreeItemRenderer.tsx`
- Detects SQLite files via `fileType === "sqlite"`
- SQLite files are expandable like folders (click to expand/collapse)
- When expanded, fetches tables from `/database/tables/{path}` API
- Tables displayed as children with table icons
- Clicking a table opens database viewer with that table pre-selected via `window.open()`

**EnvironmentPanel** - `frontend/src/components/Environment/EnvironmentPanel.tsx`
- `databaseTables` state: `Record<string, { tables: DatabaseTableItem[], loading: boolean, error: string | null }>`
- `handleFetchDatabaseTables(path)` - fetches and caches table names for a SQLite file on-demand

### Route - `frontend/src/routes/_layout/environment/$envId/database.tsx`

- URL: `/environment/{envId}/database?path={dbPath}&table={tableName}`
- Validates search params with Zod schema
- Renders `DatabaseViewer` component

## Configuration

- **Page size:** 1000 rows (default)
- **Timeout:** 30 seconds (configurable per request)
- **Supported extensions:** `.db`, `.sqlite`, `.sqlite3`

## Security

- **Path validation:** Reuses `validate_workspace_path()` - prevents directory traversal, rejects `..` and absolute paths, validates symlinks don't escape workspace
- **Query execution:** All SQL allowed; timeout prevents runaway queries; each query opens/closes connection
- **Access control:** Backend checks user owns the agent; environment must be running; agent-env validates auth token

---

*Last updated: 2026-03-02*
