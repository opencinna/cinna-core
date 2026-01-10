// Tree item types
export interface FileItem {
  type: "file"
  name: string
  fileType: string // csv, json, txt, sqlite, etc.
  size: string
  modified: string
}

export interface FolderItem {
  type: "folder"
  name: string
  size: string
  modified: string
  children: TreeItem[]
}

// Database table/view item for SQLite expansion
export interface DatabaseTableItem {
  type: "database_table"
  name: string
  tableType: "table" | "view"
  databasePath: string // Path to the parent SQLite file
}

export type TreeItem = FileItem | FolderItem


// SQLite Database Types

export interface SQLiteColumnInfo {
  name: string
  type: string // TEXT, INTEGER, REAL, BLOB, NULL
  nullable: boolean
  primary_key: boolean
}

export interface SQLiteTableInfo {
  name: string
  type: "table" | "view"
  columns: SQLiteColumnInfo[]
}

export interface SQLiteDatabaseSchema {
  path: string
  tables: SQLiteTableInfo[]
  views: SQLiteTableInfo[]
}

export interface SQLiteQueryRequest {
  path: string
  query: string
  page?: number
  page_size?: number
  timeout_seconds?: number
}

export interface SQLiteQueryResult {
  columns: string[]
  rows: unknown[][]
  total_rows: number
  page: number
  page_size: number
  has_more: boolean
  execution_time_ms: number
  query_type: "SELECT" | "INSERT" | "UPDATE" | "DELETE" | "OTHER"
  rows_affected: number | null
  error?: string | null
  error_type?: string | null
}
