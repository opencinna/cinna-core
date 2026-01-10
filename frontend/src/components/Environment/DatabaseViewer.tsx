import { useState, useEffect, useMemo } from "react"
import { useQuery, useMutation } from "@tanstack/react-query"
import {
  Download,
  Play,
  ChevronDown,
  ChevronRight,
  Table as TableIcon,
  Eye,
  Database,
  Key,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { WorkspaceService } from "@/client"
import { usePageHeader } from "@/routes/_layout"
import type {
  SQLiteDatabaseSchema,
  SQLiteQueryResult,
  SQLiteTableInfo,
} from "./types"

interface DatabaseViewerProps {
  envId: string
  dbPath: string
  initialTable?: string
}

const DEFAULT_PAGE_SIZE = 1000

export function DatabaseViewer({
  envId,
  dbPath,
  initialTable,
}: DatabaseViewerProps) {
  const { setHeaderContent } = usePageHeader()

  // State
  const [mode, setMode] = useState<"auto" | "manual">("auto")
  const [selectedTable, setSelectedTable] = useState<string | null>(
    initialTable || null
  )
  const [query, setQuery] = useState("")
  const [page, setPage] = useState(1)
  const [expandedSchema, setExpandedSchema] = useState<Set<string>>(
    new Set(["tables", "views"])
  )

  // Extract filename from path
  const filename = dbPath.split("/").pop() || "database"

  // Fetch schema
  const {
    data: schema,
    isLoading: isLoadingSchema,
    error: schemaError,
  } = useQuery({
    queryKey: ["database-schema", envId, dbPath],
    queryFn: async () => {
      const response = await WorkspaceService.getDatabaseSchema({
        envId,
        path: dbPath,
      })
      return response as unknown as SQLiteDatabaseSchema
    },
    enabled: !!envId && !!dbPath,
    staleTime: Infinity,
  })

  // Set initial table when schema loads
  useEffect(() => {
    if (schema && !selectedTable) {
      const firstTable = schema.tables[0]?.name || schema.views[0]?.name
      if (firstTable) {
        setSelectedTable(firstTable)
      }
    }
  }, [schema, selectedTable])

  // Auto-generate query for auto mode (pagination handled by backend via page/page_size params)
  const autoQuery = useMemo(() => {
    if (!selectedTable) return ""
    return `SELECT * FROM "${selectedTable}"`
  }, [selectedTable])

  // Effective query (auto or manual)
  const effectiveQuery = mode === "auto" ? autoQuery : query

  // Execute query mutation
  const queryMutation = useMutation({
    mutationFn: async () => {
      const response = await WorkspaceService.executeDatabaseQuery({
        envId,
        requestBody: {
          path: dbPath,
          query: effectiveQuery,
          // Only send pagination params in auto mode - manual mode queries handle their own LIMIT
          ...(mode === "auto"
            ? { page, page_size: DEFAULT_PAGE_SIZE }
            : {}),
          timeout_seconds: 30,
        },
      })
      return response as unknown as SQLiteQueryResult
    },
  })

  // Execute query when table/page changes in auto mode
  useEffect(() => {
    if (mode === "auto" && selectedTable && effectiveQuery) {
      queryMutation.mutate()
    }
  }, [mode, selectedTable, page])

  // Handle table click
  const handleTableClick = (tableName: string) => {
    setSelectedTable(tableName)
    setPage(1)
    if (mode === "manual") {
      setQuery(`SELECT * FROM "${tableName}" LIMIT ${DEFAULT_PAGE_SIZE}`)
    }
  }

  // Handle execute button
  const handleExecute = () => {
    if (mode === "manual" && query.trim()) {
      queryMutation.mutate()
    }
  }

  // Handle mode change
  const handleModeChange = (checked: boolean) => {
    const newMode = checked ? "manual" : "auto"
    setMode(newMode)
    if (newMode === "manual" && selectedTable) {
      setQuery(`SELECT * FROM "${selectedTable}" LIMIT ${DEFAULT_PAGE_SIZE}`)
    }
    setPage(1)
  }

  // Toggle schema section expansion
  const toggleSchemaSection = (section: string) => {
    const newExpanded = new Set(expandedSchema)
    if (newExpanded.has(section)) {
      newExpanded.delete(section)
    } else {
      newExpanded.add(section)
    }
    setExpandedSchema(newExpanded)
  }

  // Download CSV
  const handleDownloadCSV = () => {
    if (!queryMutation.data || queryMutation.data.columns.length === 0) return

    const { columns, rows } = queryMutation.data

    // Build CSV content
    const escapeCSV = (value: unknown): string => {
      const str = String(value ?? "")
      if (str.includes(",") || str.includes('"') || str.includes("\n")) {
        return `"${str.replace(/"/g, '""')}"`
      }
      return str
    }

    const csvContent = [
      columns.map(escapeCSV).join(","),
      ...rows.map((row) => row.map(escapeCSV).join(",")),
    ].join("\n")

    // Download
    const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" })
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement("a")
    link.href = url
    link.download = `${selectedTable || "query"}_export.csv`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    window.URL.revokeObjectURL(url)
  }

  // Update header
  useEffect(() => {
    setHeaderContent(
      <div className="flex items-center justify-between w-full">
        <div className="flex items-center gap-3 min-w-0">
          <Database className="h-5 w-5 text-orange-500 shrink-0" />
          <div className="min-w-0">
            <h1 className="text-base font-semibold truncate">{filename}</h1>
            <p className="text-xs text-muted-foreground truncate">{dbPath}</p>
          </div>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={handleDownloadCSV}
          disabled={
            !queryMutation.data || queryMutation.data.columns.length === 0
          }
          className="shrink-0"
        >
          <Download className="h-4 w-4 mr-2" />
          Export CSV
        </Button>
      </div>
    )
    return () => setHeaderContent(null)
  }, [filename, dbPath, queryMutation.data, setHeaderContent])

  // Loading state
  if (isLoadingSchema) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-muted-foreground">Loading database schema...</p>
      </div>
    )
  }

  // Error state
  if (schemaError) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-4">
        <p className="text-destructive">Error loading database</p>
        <p className="text-sm text-muted-foreground">
          {(schemaError as Error).message}
        </p>
      </div>
    )
  }

  if (!schema) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-muted-foreground">No schema data</p>
      </div>
    )
  }

  const result = queryMutation.data
  const isQuerying = queryMutation.isPending

  return (
    <div className="h-full flex overflow-hidden">
      {/* Schema Sidebar */}
      <div className="w-64 border-r bg-muted/30 flex flex-col shrink-0">
        <div className="p-3 border-b">
          <h2 className="text-sm font-semibold">Schema</h2>
        </div>
        <div className="flex-1 overflow-auto p-2">
          {/* Tables */}
          {schema.tables.length > 0 && (
            <SchemaSection
              title="Tables"
              items={schema.tables}
              isExpanded={expandedSchema.has("tables")}
              onToggle={() => toggleSchemaSection("tables")}
              selectedTable={selectedTable}
              onTableClick={handleTableClick}
              expandedItems={expandedSchema}
              onToggleItem={(name) => toggleSchemaSection(`table:${name}`)}
            />
          )}

          {/* Views */}
          {schema.views.length > 0 && (
            <SchemaSection
              title="Views"
              items={schema.views}
              isExpanded={expandedSchema.has("views")}
              onToggle={() => toggleSchemaSection("views")}
              selectedTable={selectedTable}
              onTableClick={handleTableClick}
              expandedItems={expandedSchema}
              onToggleItem={(name) => toggleSchemaSection(`view:${name}`)}
              isView
            />
          )}
        </div>
      </div>

      {/* Main Content */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Query Editor */}
        <div className="border-b p-4 space-y-3">
          {/* Mode Toggle */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <Label htmlFor="mode-toggle" className="text-sm font-medium">
                Mode:
              </Label>
              <div className="flex items-center gap-2">
                <span
                  className={`text-sm ${mode === "auto" ? "font-medium" : "text-muted-foreground"}`}
                >
                  Auto
                </span>
                <Switch
                  id="mode-toggle"
                  checked={mode === "manual"}
                  onCheckedChange={handleModeChange}
                />
                <span
                  className={`text-sm ${mode === "manual" ? "font-medium" : "text-muted-foreground"}`}
                >
                  Manual
                </span>
              </div>
            </div>

            {mode === "manual" && (
              <Button
                size="sm"
                onClick={handleExecute}
                disabled={!query.trim() || isQuerying}
              >
                <Play className="h-4 w-4 mr-2" />
                Execute
              </Button>
            )}
          </div>

          {/* Query Input */}
          <Textarea
            value={mode === "auto" ? autoQuery : query}
            onChange={(e) => mode === "manual" && setQuery(e.target.value)}
            placeholder="SELECT * FROM table_name"
            readOnly={mode === "auto"}
            className={`font-mono text-sm h-20 resize-none ${mode === "auto" ? "bg-muted/50" : ""}`}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                e.preventDefault()
                handleExecute()
              }
            }}
          />
          {mode === "manual" && (
            <p className="text-xs text-muted-foreground">
              Press Ctrl+Enter to execute
            </p>
          )}
        </div>

        {/* Results Area */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Status Bar */}
          <div className="border-b px-4 py-2 flex items-center justify-between text-sm bg-muted/30">
            <div className="flex items-center gap-4">
              {isQuerying && (
                <span className="text-muted-foreground">Executing...</span>
              )}
              {result && !result.error && (
                <>
                  <span>
                    {result.query_type === "SELECT"
                      ? `${result.total_rows >= 0 ? result.total_rows : "?"} rows`
                      : `${result.rows_affected ?? 0} rows affected`}
                  </span>
                  <span className="text-muted-foreground">
                    {result.execution_time_ms.toFixed(1)}ms
                  </span>
                </>
              )}
              {result?.error && (
                <span className="text-destructive">{result.error}</span>
              )}
            </div>

            {/* Pagination (Auto mode only) */}
            {mode === "auto" && result && result.query_type === "SELECT" && (
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1 || isQuerying}
                >
                  Previous
                </Button>
                <span className="text-muted-foreground">Page {page}</span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => p + 1)}
                  disabled={!result.has_more || isQuerying}
                >
                  Next
                </Button>
              </div>
            )}
          </div>

          {/* Results Table */}
          <div className="flex-1 overflow-auto">
            {result && result.columns.length > 0 && (
              <Table>
                <TableHeader>
                  <TableRow>
                    {result.columns.map((col, i) => (
                      <TableHead
                        key={i}
                        className="font-semibold whitespace-nowrap sticky top-0 bg-background"
                      >
                        {col}
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {result.rows.map((row, rowIndex) => (
                    <TableRow key={rowIndex}>
                      {row.map((cell, cellIndex) => (
                        <TableCell key={cellIndex} className="whitespace-nowrap">
                          {cell === null ? (
                            <span className="text-muted-foreground italic">
                              NULL
                            </span>
                          ) : (
                            String(cell)
                          )}
                        </TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}

            {/* Empty state */}
            {!isQuerying && (!result || result.columns.length === 0) && (
              <div className="flex items-center justify-center h-full">
                <p className="text-muted-foreground">
                  {result?.error
                    ? "Query failed"
                    : result?.query_type !== "SELECT"
                      ? "Query executed successfully"
                      : "Select a table or execute a query"}
                </p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// Schema Section Component
interface SchemaSectionProps {
  title: string
  items: SQLiteTableInfo[]
  isExpanded: boolean
  onToggle: () => void
  selectedTable: string | null
  onTableClick: (name: string) => void
  expandedItems: Set<string>
  onToggleItem: (name: string) => void
  isView?: boolean
}

function SchemaSection({
  title,
  items,
  isExpanded,
  onToggle,
  selectedTable,
  onTableClick,
  expandedItems,
  onToggleItem,
  isView,
}: SchemaSectionProps) {
  return (
    <div className="mb-2">
      <button
        onClick={onToggle}
        className="flex items-center gap-1 text-sm font-medium text-muted-foreground hover:text-foreground w-full py-1"
      >
        {isExpanded ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        {title} ({items.length})
      </button>

      {isExpanded && (
        <div className="ml-2 space-y-0.5">
          {items.map((item) => {
            const itemKey = `${isView ? "view" : "table"}:${item.name}`
            const isItemExpanded = expandedItems.has(itemKey)
            const isSelected = selectedTable === item.name

            return (
              <div key={item.name}>
                <div
                  className={`flex items-center gap-2 py-1 px-2 rounded text-sm cursor-pointer hover:bg-muted ${
                    isSelected ? "bg-muted font-medium" : ""
                  }`}
                >
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      onToggleItem(item.name)
                    }}
                    className="shrink-0"
                  >
                    {isItemExpanded ? (
                      <ChevronDown className="h-3 w-3" />
                    ) : (
                      <ChevronRight className="h-3 w-3" />
                    )}
                  </button>
                  {isView ? (
                    <Eye className="h-3.5 w-3.5 text-purple-500 shrink-0" />
                  ) : (
                    <TableIcon className="h-3.5 w-3.5 text-green-500 shrink-0" />
                  )}
                  <span
                    onClick={() => onTableClick(item.name)}
                    className="truncate flex-1"
                    title={item.name}
                  >
                    {item.name}
                  </span>
                </div>

                {/* Columns */}
                {isItemExpanded && (
                  <div className="ml-6 space-y-0.5 py-1">
                    {item.columns.map((col) => (
                      <div
                        key={col.name}
                        className="flex items-center gap-2 py-0.5 px-2 text-xs text-muted-foreground"
                      >
                        {col.primary_key && (
                          <Key className="h-3 w-3 text-yellow-500 shrink-0" />
                        )}
                        <span className="truncate" title={col.name}>
                          {col.name}
                        </span>
                        <span className="text-muted-foreground/60 shrink-0">
                          {col.type}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
