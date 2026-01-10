import { useEffect } from "react"
import { ChevronDown, ChevronRight, Folder, FolderOpen, Download, Table2, Loader2 } from "lucide-react"
import { FileIcon } from "./FileIcon"
import type { TreeItem, DatabaseTableItem } from "./types"

interface TreeItemRendererProps {
  item: TreeItem
  level: number
  expandedFolders: Set<string>
  onToggleFolder: (path: string) => void
  onDownload: (fileName: string) => void
  path?: string
  envId?: string
  databaseTables?: Record<string, { tables: DatabaseTableItem[], loading: boolean, error: string | null }>
  onFetchDatabaseTables?: (path: string) => void
}

export function TreeItemRenderer({
  item,
  level,
  expandedFolders,
  onToggleFolder,
  onDownload,
  path = "",
  envId,
  databaseTables,
  onFetchDatabaseTables,
}: TreeItemRendererProps) {
  const currentPath = path ? `${path}/${item.name}` : item.name
  const isExpanded = expandedFolders.has(currentPath)
  const paddingLeft = `${level * 12}px`

  if (item.type === "folder") {
    return (
      <>
        <div
          className="flex items-center justify-between py-1 px-2 rounded-md hover:bg-muted/50 group transition-colors cursor-pointer"
          style={{ paddingLeft }}
        >
          <div className="flex items-center gap-2 min-w-0 flex-1" onClick={() => onToggleFolder(currentPath)}>
            {isExpanded ? (
              <ChevronDown className="h-3 w-3 text-muted-foreground shrink-0" />
            ) : (
              <ChevronRight className="h-3 w-3 text-muted-foreground shrink-0" />
            )}
            {isExpanded ? (
              <FolderOpen className="h-4 w-4 text-blue-400 shrink-0" title={item.modified} />
            ) : (
              <Folder className="h-4 w-4 text-blue-400 shrink-0" title={item.modified} />
            )}
            <p className="text-sm font-medium truncate" title={item.name}>{item.name}</p>
            <span className="text-xs text-muted-foreground/60 opacity-0 group-hover:opacity-100 transition-opacity shrink-0 ml-auto mr-2">
              {item.size}
            </span>
          </div>
          <button
            className="opacity-0 group-hover:opacity-100 shrink-0 p-0 hover:text-foreground text-muted-foreground transition-colors ml-1"
            onClick={(e) => {
              e.stopPropagation()
              onDownload(currentPath)
            }}
            title="Download folder"
          >
            <Download className="h-4 w-4" />
          </button>
        </div>
        {isExpanded && item.children.map((child, index) => (
          <TreeItemRenderer
            key={index}
            item={child}
            level={level + 1}
            expandedFolders={expandedFolders}
            onToggleFolder={onToggleFolder}
            onDownload={onDownload}
            path={currentPath}
            envId={envId}
            databaseTables={databaseTables}
            onFetchDatabaseTables={onFetchDatabaseTables}
          />
        ))}
      </>
    )
  }

  // File item
  const lastDotIndex = item.name.lastIndexOf('.')
  const baseName = lastDotIndex > 0 ? item.name.substring(0, lastDotIndex) : item.name
  const extension = lastDotIndex > 0 ? item.name.substring(lastDotIndex) : ''
  const isViewableFile = item.fileType === "csv" || item.fileType === "md"
  const isSQLiteFile = item.fileType === "sqlite"
  const isClickable = isViewableFile // SQLite files are now expandable, not directly clickable

  // Get database tables data for this SQLite file
  const dbData = isSQLiteFile && databaseTables ? databaseTables[currentPath] : undefined
  const dbTables = dbData?.tables || []
  const dbLoading = dbData?.loading || false
  const dbError = dbData?.error || null

  // Fetch database tables when SQLite file is expanded
  useEffect(() => {
    if (isSQLiteFile && isExpanded && onFetchDatabaseTables && !dbData) {
      onFetchDatabaseTables(currentPath)
    }
  }, [isSQLiteFile, isExpanded, onFetchDatabaseTables, currentPath, dbData])

  const handleFileClick = () => {
    if (!envId) return

    if (isSQLiteFile) {
      // Toggle expansion like a folder
      // If currently collapsed (will expand), trigger a re-fetch to get fresh data
      if (!isExpanded && onFetchDatabaseTables) {
        onFetchDatabaseTables(currentPath)
      }
      onToggleFolder(currentPath)
    } else if (isViewableFile) {
      // Open file viewer in new tab
      const url = `/environment/${envId}/file?path=${encodeURIComponent(currentPath)}`
      window.open(url, '_blank')
    }
  }

  const handleTableClick = (tableName: string) => {
    if (!envId) return
    // Open database viewer with the specific table selected
    const url = `/environment/${envId}/database?path=${encodeURIComponent(currentPath)}&table=${encodeURIComponent(tableName)}`
    window.open(url, '_blank')
  }

  // Render SQLite file as expandable item
  if (isSQLiteFile) {
    return (
      <>
        <div
          className="flex items-center justify-between py-1 px-2 rounded-md hover:bg-muted/50 group transition-colors cursor-pointer"
          style={{ paddingLeft: `${level * 12 + 24}px` }}
        >
          <div
            className="flex items-center gap-2 min-w-0 flex-1"
            onClick={handleFileClick}
          >
            {isExpanded ? (
              <ChevronDown className="h-3 w-3 text-muted-foreground shrink-0" />
            ) : (
              <ChevronRight className="h-3 w-3 text-muted-foreground shrink-0" />
            )}
            <div className="shrink-0" title={item.modified}>
              <FileIcon fileType={item.fileType} />
            </div>
            <p className="text-sm font-medium truncate" title={item.name}>
              {baseName}<span className="text-muted-foreground/60 opacity-0 group-hover:opacity-100 transition-opacity">{extension}</span>
            </p>
            <span className="text-xs text-muted-foreground/60 opacity-0 group-hover:opacity-100 transition-opacity shrink-0 ml-auto">
              {item.size}
            </span>
            <button
              className="opacity-0 group-hover:opacity-100 shrink-0 p-0 hover:text-foreground text-muted-foreground transition-colors"
              onClick={(e) => {
                e.stopPropagation()
                onDownload(currentPath)
              }}
              title="Download"
            >
              <Download className="h-4 w-4" />
            </button>
          </div>
        </div>
        {/* Render database tables when expanded */}
        {isExpanded && (
          <>
            {dbLoading && (
              <div
                className="flex items-center gap-2 py-1 px-2 text-muted-foreground"
                style={{ paddingLeft: `${(level + 1) * 12 + 48}px` }}
              >
                <Loader2 className="h-3 w-3 animate-spin" />
                <span className="text-xs">Loading tables...</span>
              </div>
            )}
            {dbError && (
              <div
                className="flex items-center gap-2 py-1 px-2 text-destructive"
                style={{ paddingLeft: `${(level + 1) * 12 + 48}px` }}
              >
                <span className="text-xs">Error loading tables</span>
              </div>
            )}
            {!dbLoading && !dbError && dbTables.length === 0 && (
              <div
                className="flex items-center gap-2 py-1 px-2 text-muted-foreground"
                style={{ paddingLeft: `${(level + 1) * 12 + 48}px` }}
              >
                <span className="text-xs">No tables found</span>
              </div>
            )}
            {dbTables.map((table, index) => (
              <div
                key={index}
                className="flex items-center gap-2 py-1 px-2 rounded-md hover:bg-muted/50 cursor-pointer transition-colors"
                style={{ paddingLeft: `${(level + 1) * 12 + 48}px` }}
                onClick={() => handleTableClick(table.name)}
              >
                <Table2
                  className={`h-4 w-4 shrink-0 ${table.tableType === "view" ? "text-purple-500" : "text-blue-400"}`}
                  title={table.tableType === "view" ? "View" : "Table"}
                />
                <span className="text-sm">{table.name}</span>
              </div>
            ))}
          </>
        )}
      </>
    )
  }

  // Regular file item (non-SQLite)
  return (
    <div
      className="flex items-center justify-between py-1 px-2 rounded-md hover:bg-muted/50 group transition-colors"
      style={{ paddingLeft: `${level * 12 + 24}px` }} // Extra padding for files to align with folder content
    >
      <div
        className={`flex items-center gap-2 min-w-0 flex-1 ${isClickable && envId ? "cursor-pointer" : ""}`}
        onClick={handleFileClick}
      >
        <div className="shrink-0" title={item.modified}>
          <FileIcon fileType={item.fileType} />
        </div>
        <p className="text-sm font-medium truncate" title={item.name}>
          {baseName}<span className="text-muted-foreground/60 opacity-0 group-hover:opacity-100 transition-opacity">{extension}</span>
        </p>
        <span className="text-xs text-muted-foreground/60 opacity-0 group-hover:opacity-100 transition-opacity shrink-0 ml-auto">
          {item.size}
        </span>
        <button
          className="opacity-0 group-hover:opacity-100 shrink-0 p-0 hover:text-foreground text-muted-foreground transition-colors"
          onClick={(e) => {
            e.stopPropagation()
            onDownload(currentPath)
          }}
          title="Download"
        >
          <Download className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}
