import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Ban, Globe, ListFilter, Terminal } from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import type { McpDirectoryEntryPublic } from "@/client"
import { BanEntryDialog } from "@/components/Admin/Addons/BanEntryDialog"
import { AddMcpServerDialog } from "@/components/Admin/McpDirectory/AddMcpServerDialog"
import { DeleteMcpServerDialog } from "@/components/Admin/McpDirectory/DeleteMcpServerDialog"
import { EditMcpServerSheet } from "@/components/Admin/McpDirectory/EditMcpServerSheet"
import { ImportMcpDirectoryDialog } from "@/components/Admin/McpDirectory/ImportMcpDirectoryDialog"
import {
  isRowPending,
  useMcpDirectoryEntries,
  useSetEntryAccess,
} from "@/components/Admin/McpDirectory/mcpDirectoryApi"
import {
  entryTitleId,
  getMcpDirectoryColumns,
} from "@/components/Admin/McpDirectory/mcpDirectoryColumns"
import { DataTable } from "@/components/Common/DataTable"
import { FilterMenu, type FilterOption } from "@/components/Common/FilterMenu"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import {
  TableEmptyBlock,
  TableSearchInput,
} from "@/components/Common/TableSearchInput"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/admin/mcp-directory")({
  component: AdminMcpDirectory,
  head: () => ({
    meta: [{ title: `MCP Directory - Admin - ${APP_NAME}` }],
  }),
})

type Entry = McpDirectoryEntryPublic
type ServerFilter = "all" | "remote" | "local" | "banned"

const FILTERS: readonly FilterOption<ServerFilter>[] = [
  { value: "all", label: "All servers", icon: ListFilter },
  { value: "remote", label: "Remote", icon: Globe },
  { value: "local", label: "Local process", icon: Terminal },
  { value: "banned", label: "Banned", icon: Ban },
]

const BAN_EFFECT =
  "Users can no longer find or add it. Agents that already use it keep their copy."

function matchesFilter(entry: Entry, filter: ServerFilter): boolean {
  if (filter === "remote") return entry.transport !== "stdio"
  if (filter === "local") return entry.transport === "stdio"
  if (filter === "banned") return entry.access === "banned"
  return true
}

function matchesQuery(entry: Entry, query: string): boolean {
  return [entry.name, entry.title, entry.publisher_name]
    .filter(Boolean)
    .some((field) => (field as string).toLowerCase().includes(query))
}

function PendingRows() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 5 }).map((_, index) => (
        <Skeleton key={index} className="h-[48px] w-full" />
      ))}
    </div>
  )
}

/** Mount-per-open state for a dialog: `key` changes on every open. */
function useDialogSession() {
  const [open, setOpen] = useState(false)
  const [key, setKey] = useState(0)
  const show = useCallback(() => {
    setKey((k) => k + 1)
    setOpen(true)
  }, [])
  return { open, key, show, setOpen }
}

function McpDirectoryList({
  onAdd,
  onImport,
}: {
  onAdd: () => void
  onImport: () => void
}) {
  const entries = useMcpDirectoryEntries()
  const setAccess = useSetEntryAccess()
  const queryClient = useQueryClient()
  const [search, setSearch] = useState("")
  const searchRef = useRef<HTMLInputElement>(null)
  const [filter, setFilter] = useState<ServerFilter>("all")
  const [editTarget, setEditTarget] = useState<Entry | null>(null)
  const [banTarget, setBanTarget] = useState<Entry | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Entry | null>(null)

  const clearSearch = () => {
    setSearch("")
    searchRef.current?.focus()
  }
  const clearFilters = () => {
    setFilter("all")
    clearSearch()
  }

  const columns = useMemo(
    () =>
      getMcpDirectoryColumns({
        onEdit: setEditTarget,
        onBan: setBanTarget,
        onDelete: setDeleteTarget,
        onUnban: (entry) => {
          setAccess.mutate({ entry, access: "default" })
          // The menu trigger becomes the pending spinner; focus goes to the
          // row's title once the menu has handed it back to the trigger.
          setTimeout(
            () => document.getElementById(entryTitleId(entry.id))?.focus(),
            0,
          )
        },
      }),
    [setAccess.mutate],
  )

  const query = search.trim().toLowerCase()
  const rows = useMemo(
    () =>
      (entries.data?.data ?? []).filter(
        (entry) =>
          matchesFilter(entry, filter) &&
          (!query || matchesQuery(entry, query)),
      ),
    [entries.data, filter, query],
  )

  if (entries.isError && entries.data === undefined) {
    return (
      <QueryErrorAlert
        error={entries.error}
        fallback="Couldn't load the MCP directory"
        onRetry={() => entries.refetch()}
      />
    )
  }

  if (entries.data === undefined) return <PendingRows />

  if (entries.data.data.length === 0) {
    return (
      <div className="flex flex-col items-center gap-3 py-16 text-center">
        <p className="text-sm text-muted-foreground">
          No MCP servers in the directory yet.
        </p>
        <div className="flex flex-wrap justify-center gap-2">
          <Button variant="outline" onClick={onImport}>
            Import from opencinna
          </Button>
          <Button onClick={onAdd}>Add MCP server</Button>
        </div>
      </div>
    )
  }

  // Any entry still in the list — the targets are re-read so a refetch that
  // changes a row reaches its open dialog.
  const latest = (target: Entry | null) =>
    target
      ? (entries.data?.data.find((e) => e.id === target.id) ?? target)
      : null
  const banEntry = latest(banTarget)
  const deleteEntry = latest(deleteTarget)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <TableSearchInput
          ref={searchRef}
          value={search}
          onChange={setSearch}
          onClear={clearSearch}
          placeholder="Search servers"
          ariaLabel="Search servers by name, title or publisher"
        />
        <FilterMenu
          options={FILTERS}
          value={filter}
          onChange={setFilter}
          allValue="all"
        />
      </div>

      <DataTable
        // A new filter or search starts at page 1; a refetch keeps the page.
        key={`${filter}|${query}`}
        autoResetPageIndex={false}
        columns={columns}
        data={rows}
        getRowId={(entry) => entry.id}
        tableClassName="table-fixed"
        itemLabel="servers"
        emptyState={
          <TableEmptyBlock text="No servers match.">
            <Button variant="link" size="sm" onClick={clearFilters}>
              Clear filters
            </Button>
          </TableEmptyBlock>
        }
        onRowClick={(entry) => {
          // A row with a write in flight is not a way into it.
          if (!isRowPending(queryClient, entry.id)) setEditTarget(entry)
        }}
      />

      {editTarget && (
        <EditMcpServerSheet
          key={editTarget.id}
          entry={editTarget}
          onClose={() => setEditTarget(null)}
        />
      )}

      {banEntry && (
        <BanEntryDialog
          open
          onOpenChange={(open) => {
            if (!open) setBanTarget(null)
          }}
          entryLabel={banEntry.title}
          effectSentence={BAN_EFFECT}
          confirmLabel="Ban server"
          returnFocusId={entryTitleId(banEntry.id)}
          onConfirm={(reason) =>
            setAccess.mutateAsync({ entry: banEntry, access: "banned", reason })
          }
        />
      )}

      {deleteEntry && (
        <DeleteMcpServerDialog
          entry={deleteEntry}
          onClose={() => setDeleteTarget(null)}
          onReturnFocus={(deleted) => {
            const title = deleted
              ? null
              : document.getElementById(entryTitleId(deleteEntry.id))
            ;(title ?? searchRef.current)?.focus()
          }}
        />
      )}
    </div>
  )
}

function AdminMcpDirectory() {
  const { setHeaderContent } = usePageHeader()
  const add = useDialogSession()
  const importer = useDialogSession()
  const { show: showAdd } = add
  const { show: showImport } = importer

  useEffect(() => {
    setHeaderContent(
      <>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">MCP Directory</h1>
          <p className="text-xs text-muted-foreground">
            Servers users can add to their agents
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="outline" onClick={showImport}>
            Import from opencinna
          </Button>
          <Button onClick={showAdd}>Add MCP server</Button>
        </div>
      </>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, showAdd, showImport])

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        <McpDirectoryList onAdd={showAdd} onImport={showImport} />
      </div>
      <AddMcpServerDialog
        key={`add-${add.key}`}
        open={add.open}
        onOpenChange={add.setOpen}
      />
      <ImportMcpDirectoryDialog
        key={`import-${importer.key}`}
        open={importer.open}
        onOpenChange={importer.setOpen}
      />
    </div>
  )
}
