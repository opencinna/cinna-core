import { useState } from "react"
import {
  useReactTable,
  getCoreRowModel,
  flexRender,
  createColumnHelper,
  type OnChangeFn,
  type RowSelectionState,
} from "@tanstack/react-table"
import type { AdminAgentEnvironmentPublic } from "@/client"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Checkbox } from "@/components/ui/checkbox"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { Copy, Check, AlertTriangle } from "lucide-react"
import { AdminEnvBulkRebuildDialog } from "./AdminEnvBulkRebuildDialog"

// ---------------------------------------------------------------------------
// Status badge helpers
// ---------------------------------------------------------------------------

export const TRANSITIONAL_STATUSES = new Set([
  "creating",
  "building",
  "initializing",
  "starting",
  "rebuilding",
  "activating",
])

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    running:
      "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    stopped:
      "bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300",
    suspended:
      "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
    error: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200",
    deprecated:
      "bg-muted text-muted-foreground",
  }
  const amber =
    "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200"
  const cls = TRANSITIONAL_STATUSES.has(status)
    ? amber
    : (colors[status] ?? amber)

  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}
    >
      {TRANSITIONAL_STATUSES.has(status) && (
        <span className="relative flex h-2 w-2">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
          <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-500" />
        </span>
      )}
      {status}
    </span>
  )
}

function StaleBadge({
  isStale,
  currentTag,
  expectedTag,
}: {
  isStale: boolean
  currentTag: string | null
  expectedTag: string | null
}) {
  if (!isStale) return null
  const shortCurrent = currentTag?.split(":").pop()?.slice(0, 12) ?? "none"
  const shortExpected = expectedTag?.split(":").pop()?.slice(0, 12) ?? "unknown"
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200 cursor-default">
          Stale
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <p className="font-mono text-xs">
          current: {shortCurrent}
        </p>
        <p className="font-mono text-xs">
          expected: {shortExpected}
        </p>
      </TooltipContent>
    </Tooltip>
  )
}

function ModelHealthCell({ warning }: { warning: boolean }) {
  if (!warning) return <span className="text-xs text-muted-foreground">OK</span>
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200 cursor-default">
          <AlertTriangle className="h-3 w-3" />
          Model
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <p className="text-xs">
          A configured AI model is deprecated or unavailable. Owner should
          reconfigure / restart this environment.
        </p>
      </TooltipContent>
    </Tooltip>
  )
}

function InUseBadge({ inUse, count }: { inUse: boolean; count: number }) {
  if (!inUse) return <span className="text-xs text-muted-foreground">No</span>
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex items-center gap-1 text-xs font-medium text-blue-700 dark:text-blue-300 cursor-default">
          <span className="relative flex h-2 w-2">
            <span className="animate-pulse absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-blue-500" />
          </span>
          In use
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <p className="text-xs">{count} active session{count !== 1 ? "s" : ""}</p>
      </TooltipContent>
    </Tooltip>
  )
}

function ImageTagCell({ tag }: { tag: string | null }) {
  const [copied, setCopied] = useState(false)
  if (!tag) return <span className="text-muted-foreground text-xs">—</span>
  const tagParts = tag.split(":")
  const shortHash = (tagParts.length > 1 ? tagParts[tagParts.length - 1] ?? tag : tag).slice(0, 12)

  const handleCopy = () => {
    void navigator.clipboard.writeText(tag)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="group inline-flex items-center gap-1 font-mono text-xs cursor-default">
          {shortHash}
          <button
            onClick={handleCopy}
            className="opacity-0 group-hover:opacity-100 transition-opacity ml-0.5"
            aria-label="Copy image tag"
          >
            {copied ? (
              <Check className="h-3 w-3 text-emerald-500" />
            ) : (
              <Copy className="h-3 w-3 text-muted-foreground" />
            )}
          </button>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <p className="font-mono text-xs">{tag}</p>
      </TooltipContent>
    </Tooltip>
  )
}

function FormattedDate({ date }: { date: string | null }) {
  if (!date) return <span className="text-muted-foreground text-xs">—</span>
  const d = new Date(date)
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="text-xs cursor-default">
          {d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}{" "}
          {d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <p className="text-xs">{d.toISOString()}</p>
      </TooltipContent>
    </Tooltip>
  )
}

// ---------------------------------------------------------------------------
// Column definitions
// ---------------------------------------------------------------------------

const columnHelper = createColumnHelper<AdminAgentEnvironmentPublic>()

const columns = [
  columnHelper.display({
    id: "select",
    header: ({ table }) => (
      <Checkbox
        checked={
          table.getIsAllPageRowsSelected() ||
          (table.getIsSomePageRowsSelected() && "indeterminate")
        }
        onCheckedChange={(v) => table.toggleAllPageRowsSelected(!!v)}
        aria-label="Select all"
      />
    ),
    cell: ({ row }) => {
      const isTransitional = TRANSITIONAL_STATUSES.has(row.original.status)
      return (
        <Checkbox
          checked={row.getIsSelected()}
          onCheckedChange={(v) => row.toggleSelected(!!v)}
          disabled={isTransitional}
          aria-label="Select row"
        />
      )
    },
    size: 40,
    enableSorting: false,
  }),
  columnHelper.accessor("agent_name", {
    header: "Agent",
    cell: ({ row }) => (
      <div>
        <p className="text-sm font-medium truncate max-w-[160px]">
          {row.original.agent_name}
        </p>
        <p className="text-xs text-muted-foreground truncate max-w-[160px]">
          {row.original.owner_email}
        </p>
      </div>
    ),
  }),
  columnHelper.accessor("instance_name", {
    header: "Instance",
    cell: (info) => (
      <span className="text-sm truncate max-w-[120px] block">
        {info.getValue()}
      </span>
    ),
  }),
  columnHelper.accessor("env_name", {
    header: "Template",
    cell: (info) => (
      <Badge variant="outline" className="text-xs font-mono">
        {info.getValue()}
      </Badge>
    ),
  }),
  columnHelper.accessor("status", {
    header: "Status",
    cell: (info) => <StatusBadge status={info.getValue()} />,
  }),
  columnHelper.accessor("in_use", {
    header: "In use",
    cell: ({ row }) => (
      <InUseBadge
        inUse={row.original.in_use}
        count={row.original.active_sessions_count}
      />
    ),
  }),
  columnHelper.accessor("is_stale", {
    header: "Stale",
    cell: ({ row }) => (
      <StaleBadge
        isStale={row.original.is_stale}
        currentTag={row.original.current_image_tag ?? null}
        expectedTag={row.original.expected_image_tag ?? null}
      />
    ),
  }),
  columnHelper.accessor("model_health_warning", {
    header: "Model",
    cell: ({ row }) => (
      <ModelHealthCell warning={row.original.model_health_warning ?? false} />
    ),
  }),
  columnHelper.accessor("current_image_tag", {
    header: "Current tag",
    cell: (info) => <ImageTagCell tag={info.getValue() ?? null} />,
  }),
  columnHelper.accessor("expected_image_tag", {
    header: "Expected tag",
    cell: (info) => <ImageTagCell tag={info.getValue() ?? null} />,
  }),
  columnHelper.accessor("last_build_at", {
    header: "Last built",
    cell: (info) => <FormattedDate date={info.getValue() ?? null} />,
  }),
  columnHelper.accessor("last_activity_at", {
    header: "Last activity",
    cell: (info) => <FormattedDate date={info.getValue() ?? null} />,
  }),
]

// ---------------------------------------------------------------------------
// Table component
// ---------------------------------------------------------------------------

interface AdminEnvTableProps {
  data: AdminAgentEnvironmentPublic[]
  onRebuildSelected: (ids: string[]) => Promise<void>
  isRebuildPending: boolean
  rowSelection: RowSelectionState
  onRowSelectionChange: OnChangeFn<RowSelectionState>
}

export function AdminEnvTable({
  data,
  onRebuildSelected,
  isRebuildPending,
  rowSelection,
  onRowSelectionChange,
}: AdminEnvTableProps) {
  const [confirmOpen, setConfirmOpen] = useState(false)

  const table = useReactTable({
    data,
    columns,
    getCoreRowModel: getCoreRowModel(),
    state: { rowSelection },
    onRowSelectionChange,
    enableRowSelection: (row) => !TRANSITIONAL_STATUSES.has(row.original.status),
    getRowId: (row) => row.id,
  })

  const selectedRows = table
    .getSelectedRowModel()
    .rows.map((r) => r.original)

  const handleRebuildConfirm = async () => {
    await onRebuildSelected(selectedRows.map((r) => r.id))
    setConfirmOpen(false)
  }



  return (
    <div className="space-y-2">
      {/* Bulk actions bar */}
      {selectedRows.length > 0 && (
        <div className="flex items-center gap-3 rounded-md border bg-muted/50 px-4 py-2 text-sm">
          <span className="text-muted-foreground">
            {selectedRows.length} env{selectedRows.length !== 1 ? "s" : ""} selected
          </span>
          <Button
            size="sm"
            onClick={() => setConfirmOpen(true)}
            disabled={isRebuildPending}
          >
            Rebuild Selected
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => onRowSelectionChange({})}
          >
            Clear
          </Button>
        </div>
      )}

      {/* Table */}
      <div className="rounded-md border overflow-x-auto">
        <Table>
          <TableHeader className="sticky top-0 bg-background z-10">
            {table.getHeaderGroups().map((hg) => (
              <TableRow key={hg.id}>
                {hg.headers.map((header) => (
                  <TableHead key={header.id} className="whitespace-nowrap text-xs">
                    {header.isPlaceholder
                      ? null
                      : flexRender(
                          header.column.columnDef.header,
                          header.getContext()
                        )}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {table.getRowModel().rows.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={columns.length}
                  className="h-32 text-center text-muted-foreground text-sm"
                >
                  No environments found.
                </TableCell>
              </TableRow>
            ) : (
              table.getRowModel().rows.map((row) => {
                const isTransitional = TRANSITIONAL_STATUSES.has(
                  row.original.status
                )
                return (
                  <TableRow
                    key={row.id}
                    data-state={row.getIsSelected() ? "selected" : undefined}
                    className={isTransitional ? "opacity-60" : undefined}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <TableCell key={cell.id} className="py-2">
                        {flexRender(
                          cell.column.columnDef.cell,
                          cell.getContext()
                        )}
                      </TableCell>
                    ))}
                  </TableRow>
                )
              })
            )}
          </TableBody>
        </Table>
      </div>

      {/* Bulk rebuild confirm dialog */}
      <AdminEnvBulkRebuildDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        selectedEnvs={selectedRows}
        onConfirm={handleRebuildConfirm}
        isPending={isRebuildPending}
      />
    </div>
  )
}

// Export selectAllStale helper so the route can call it
export type { AdminEnvTableProps }
