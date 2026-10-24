import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import type { RowSelectionState } from "@tanstack/react-table"
import {
  AlertCircle,
  Ban,
  CircleCheck,
  Clock,
  Hourglass,
  ListFilter,
  Loader2,
  RotateCcw,
} from "lucide-react"
import { useMemo, useRef, useState } from "react"

import type {
  LLMPluginMarketplacePluginPublic,
  LLMPluginMarketplacePublic,
} from "@/client"
import { LlmPluginsService } from "@/client"
import { BanEntryDialog } from "@/components/Admin/Addons/BanEntryDialog"
import { DataTable } from "@/components/Common/DataTable"
import { DEFAULT_PAGE_SIZE } from "@/components/Common/DataTablePagination"
import { FilterMenu, type FilterOption } from "@/components/Common/FilterMenu"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import {
  TableEmptyBlock,
  TableSearchInput,
  useDebouncedValue,
} from "@/components/Common/TableSearchInput"
import { Button } from "@/components/ui/button"
import { LoadingButton } from "@/components/ui/loading-button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { marketplaceEntriesKey } from "@/hooks/useMarketplaceSync"
import { marketplaceAccessMode, plural } from "@/utils/marketplace"
import {
  BULK_ACCESS_LIMIT,
  isEntryPending,
  useAllowEntries,
  useSetEntryAccess,
} from "./marketplaceEntryAccess"
import {
  entryNameId,
  getMarketplaceEntryColumns,
} from "./marketplaceEntryColumns"

/** How often the list re-reads while any snapshot is still being taken. */
const PENDING_POLL_MS = 5_000

type Entry = LLMPluginMarketplacePluginPublic
type EntryFilter =
  | "all"
  | "waiting"
  | "allowed"
  | "banned"
  | "pending"
  | "failed"

const WAITING_FILTER: FilterOption<EntryFilter> = {
  value: "waiting",
  label: "Waiting for approval",
  icon: Hourglass,
}

const FILTERS: readonly FilterOption<EntryFilter>[] = [
  { value: "all", label: "All entries", icon: ListFilter },
  { value: "allowed", label: "Allowed", icon: CircleCheck },
  { value: "banned", label: "Banned", icon: Ban },
  { value: "pending", label: "Snapshot pending", icon: Clock },
  { value: "failed", label: "Snapshot failed", icon: AlertCircle },
]

/** "Waiting for approval" only means something in allowlist mode. */
const ALLOWLIST_FILTERS: readonly FilterOption<EntryFilter>[] = [
  FILTERS[0],
  WAITING_FILTER,
  ...FILTERS.slice(1),
]

/** The one query param each filter maps to. */
function filterParams(filter: EntryFilter) {
  if (filter === "waiting") return { waiting: true }
  if (filter === "allowed" || filter === "banned") return { access: filter }
  if (filter === "pending" || filter === "failed") {
    return { snapshotStatus: filter }
  }
  return {}
}

const BAN_EFFECT =
  "It disappears for every user, including from Cinna Desktop's picker. Agents that already installed it keep their copy, and desktop pins keep working from cache but stop updating."

interface MarketplacePluginsTabProps {
  marketplace: LLMPluginMarketplacePublic
  marketplaceId: string
}

function PendingEntries() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 5 }).map((_, index) => (
        <Skeleton key={index} className="h-[48px] w-full" />
      ))}
    </div>
  )
}

/** A sentence that names the Configuration tab links to it (§2 Empty state). */
function OpenConfiguration({ text }: { text: string }) {
  const navigate = useNavigate()
  return (
    <TableEmptyBlock text={text}>
      <Button
        variant="link"
        size="sm"
        onClick={() => void navigate({ hash: "configuration" })}
      >
        Open Configuration
      </Button>
    </TableEmptyBlock>
  )
}

function useRetrySnapshots(marketplaceId: string, onDone: () => void) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  return useMutation({
    mutationFn: () =>
      LlmPluginsService.retryMarketplaceSnapshots({ marketplaceId }),
    onSuccess: ({ queued, skipped_needs_sync }) => {
      const skipped = skipped_needs_sync ?? 0
      const parts = [
        queued > 0 ? `Queued ${plural(queued, "snapshot")}` : null,
        skipped > 0
          ? `${plural(skipped, "entry", "entries")} need a sync — sync the marketplace on Configuration`
          : null,
      ].filter(Boolean)
      // Both zero only when another admin retried first.
      showSuccessToast(parts.join(". ") || "No failed fetches to retry")
      queryClient.invalidateQueries({
        queryKey: marketplaceEntriesKey(marketplaceId),
      })
      onDone()
    },
    onError: (error) => {
      showErrorToast(error.message || "Couldn't queue the failed snapshots")
    },
  })
}

/**
 * S1 — every entry of this marketplace, searched, filtered and paged on the
 * server: whether its snapshot is ready, and what it ships. A P9 list, not a
 * card: it needs search and paging (§2 Card width).
 */
export function MarketplacePluginsTab({
  marketplace,
  marketplaceId,
}: MarketplacePluginsTabProps) {
  const navigate = useNavigate()
  const [search, setSearch] = useState("")
  const searchRef = useRef<HTMLInputElement>(null)
  const q = useDebouncedValue(search.trim(), 300)
  const mode = marketplaceAccessMode(marketplace.access_mode)
  const [filter, setFilter] = useState<EntryFilter>("all")
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({})
  // Waiting is not an option once the mode leaves allowlist; the list falls
  // back to all entries (adjusted during render), and the selection made
  // under the old filter goes with it.
  if (filter === "waiting" && mode !== "allowlist") {
    setFilter("all")
    setRowSelection({})
  }
  const [banTarget, setBanTarget] = useState<Entry | null>(null)
  const [pageIndex, setPageIndex] = useState(0)
  // Back to page 1 when the *settled* search changes, not per keystroke:
  // resetting on input would fetch the old search at page 1 first. Adjusted
  // during render (React's "storing information from previous renders"), so
  // no request goes out with the new query on the old page either.
  const [pagedQ, setPagedQ] = useState(q)
  if (pagedQ !== q) {
    setPagedQ(q)
    setPageIndex(0)
    // A selection is of the list the admin was looking at.
    setRowSelection({})
  }
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  // The Retry button unmounts once nothing is left to retry; focus goes to
  // the search input rather than falling to the page.
  const retry = useRetrySnapshots(marketplaceId, () =>
    searchRef.current?.focus(),
  )
  const connected = marketplace.status === "connected"

  const entries = useQuery({
    queryKey: [
      ...marketplaceEntriesKey(marketplaceId),
      filter,
      q,
      pageIndex,
      pageSize,
    ],
    queryFn: () =>
      LlmPluginsService.listMarketplaceEntries({
        marketplaceId,
        ...filterParams(filter),
        search: q || undefined,
        skip: pageIndex * pageSize,
        limit: pageSize,
      }),
    placeholderData: keepPreviousData,
    enabled: connected,
    // `counts` covers the whole marketplace, so the list keeps re-reading
    // while any snapshot anywhere in it is still being taken — a filtered
    // page would otherwise never see the row it is waiting for arrive.
    refetchInterval: (query) =>
      (query.state.data?.counts?.pending ?? 0) > 0 ? PENDING_POLL_MS : false,
  })

  const queryClient = useQueryClient()
  const setAccess = useSetEntryAccess(marketplaceId)
  const selectedIds = Object.keys(rowSelection).filter((id) => rowSelection[id])
  const allow = useAllowEntries(marketplaceId, () => {
    setRowSelection({})
    // The Allow button unmounts with the selection toolbar.
    searchRef.current?.focus()
  })

  const columns = useMemo(() => {
    // The menu trigger becomes the row's pending spinner; focus goes to the
    // row's name once the menu has handed it back to the trigger.
    const focusName = (entry: Entry) =>
      setTimeout(() => document.getElementById(entryNameId(entry.id))?.focus())
    return getMarketplaceEntryColumns({
      mode,
      actions: {
        onAllow: (entry) => {
          setAccess.mutate({ entry, access: "allowed" })
          focusName(entry)
        },
        onBan: setBanTarget,
        onReset: (entry) => {
          setAccess.mutate({ entry, access: "default" })
          focusName(entry)
        },
        onOpen: (entry) =>
          void navigate({
            to: "/admin/marketplace/plugin/$pluginId",
            params: { pluginId: entry.id },
          }),
      },
    })
  }, [mode, setAccess.mutate, navigate])

  const changeSearch = (value: string) => setSearch(value)
  const changeFilter = (next: EntryFilter) => {
    setFilter(next)
    setPageIndex(0)
    setRowSelection({})
  }
  // Both clear controls unmount on click, so focus goes to the input.
  const clearSearch = () => {
    changeSearch("")
    searchRef.current?.focus()
  }
  const clearFilters = () => {
    setFilter("all")
    clearSearch()
  }

  // Exactly what the default retry would queue (fetch failures that need no
  // sync), so the button never shows for content failures it can't fix.
  const retryable = entries.data?.counts?.retryable ?? 0
  const narrowed = filter !== "all" || q !== ""
  const overLimit = selectedIds.length > BULK_ACCESS_LIMIT
  const pageIds = new Set((entries.data?.data ?? []).map((entry) => entry.id))
  const offPage = selectedIds.filter((id) => !pageIds.has(id)).length

  let body: React.ReactNode
  if (!connected) {
    body = (
      <OpenConfiguration text="This marketplace isn't connected yet. Sync it on the Configuration tab to load its entries." />
    )
  } else if (entries.isError && entries.data === undefined) {
    body = (
      <QueryErrorAlert
        error={entries.error}
        fallback="Couldn't load this marketplace's entries"
        onRetry={() => entries.refetch()}
      />
    )
  } else if (entries.data === undefined) {
    body = <PendingEntries />
  } else if (entries.data.count === 0 && !narrowed) {
    body = (
      <OpenConfiguration text="This marketplace has no entries. Sync it on the Configuration tab to fetch them." />
    )
  } else {
    body = (
      <DataTable
        columns={columns}
        data={entries.data.data}
        getRowId={(entry) => entry.id}
        tableClassName="table-fixed"
        itemLabel="entries"
        rowSelection={rowSelection}
        onRowSelectionChange={setRowSelection}
        serverPagination={{
          pageIndex,
          pageSize,
          total: entries.data.count,
          onPageChange: setPageIndex,
          onPageSizeChange: setPageSize,
        }}
        emptyState={
          <TableEmptyBlock
            text={
              filter === "waiting" && q === ""
                ? "Nothing is waiting for approval."
                : "No entries match."
            }
          >
            <Button variant="link" size="sm" onClick={clearFilters}>
              Clear filters
            </Button>
          </TableEmptyBlock>
        }
        onRowClick={(entry) => {
          // A row with a write in flight is not a way into it.
          if (isEntryPending(queryClient, entry.id)) return
          void navigate({
            to: "/admin/marketplace/plugin/$pluginId",
            params: { pluginId: entry.id },
          })
        }}
      />
    )
  }

  return (
    <div className="space-y-4">
      {connected && (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <TableSearchInput
            ref={searchRef}
            value={search}
            onChange={changeSearch}
            onClear={clearSearch}
            placeholder="Search entries"
            ariaLabel="Search entries by name or description"
          />
          {selectedIds.length > 0 ? (
            <div className="flex items-center gap-2">
              <span
                className="text-sm text-muted-foreground"
                aria-live="polite"
              >
                {selectedIds.length} selected
                {offPage > 0 && " across pages"}
              </span>
              {overLimit ? (
                <Tooltip>
                  <TooltipTrigger asChild>
                    {/* A disabled button swallows hover; the wrapper owns
                        the tooltip. */}
                    <span className="inline-flex">
                      <Button size="sm" disabled>
                        Allow {selectedIds.length}
                      </Button>
                    </span>
                  </TooltipTrigger>
                  <TooltipContent>
                    Select at most {BULK_ACCESS_LIMIT}
                  </TooltipContent>
                </Tooltip>
              ) : (
                <LoadingButton
                  size="sm"
                  loading={allow.isPending}
                  onClick={() => allow.mutate(selectedIds)}
                >
                  {allow.isPending
                    ? `Allowing ${selectedIds.length}…`
                    : `Allow ${selectedIds.length}`}
                </LoadingButton>
              )}
              <Button
                variant="ghost"
                size="sm"
                disabled={allow.isPending}
                onClick={() => {
                  setRowSelection({})
                  searchRef.current?.focus()
                }}
              >
                Clear
              </Button>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <FilterMenu
                options={mode === "allowlist" ? ALLOWLIST_FILTERS : FILTERS}
                value={filter}
                onChange={changeFilter}
                allValue="all"
              />
              {retryable > 0 && (
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-1.5"
                  disabled={retry.isPending}
                  onClick={() => retry.mutate()}
                >
                  {retry.isPending ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <RotateCcw className="h-3.5 w-3.5" />
                  )}
                  {retry.isPending ? "Queuing…" : "Retry failed snapshots"}
                </Button>
              )}
            </div>
          )}
        </div>
      )}
      {body}

      {banTarget && (
        <BanEntryDialog
          open
          onOpenChange={(open) => {
            if (!open) setBanTarget(null)
          }}
          entryLabel={banTarget.name}
          effectSentence={BAN_EFFECT}
          confirmLabel="Ban plugin"
          returnFocusId={entryNameId(banTarget.id)}
          onConfirm={(reason) =>
            setAccess.mutateAsync({
              entry: banTarget,
              access: "banned",
              reason,
            })
          }
        />
      )}
    </div>
  )
}
