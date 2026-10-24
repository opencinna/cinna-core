import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import {
  AlertCircle,
  Clock,
  ListFilter,
  Loader2,
  RotateCcw,
} from "lucide-react"
import { useMemo, useRef, useState } from "react"

import type { LLMPluginMarketplacePublic } from "@/client"
import { LlmPluginsService } from "@/client"
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
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { marketplaceEntriesKey } from "@/hooks/useMarketplaceSync"
import { plural } from "@/utils/marketplace"
import { getMarketplaceEntryColumns } from "./marketplaceEntryColumns"

/** How often the list re-reads while any snapshot is still being taken. */
const PENDING_POLL_MS = 5_000

type EntryFilter = "all" | "pending" | "failed"

// K5 inserts the access options between "All entries" and the snapshot ones.
const FILTERS: readonly FilterOption<EntryFilter>[] = [
  { value: "all", label: "All entries", icon: ListFilter },
  { value: "pending", label: "Snapshot pending", icon: Clock },
  { value: "failed", label: "Snapshot failed", icon: AlertCircle },
]

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
  const [filter, setFilter] = useState<EntryFilter>("all")
  const [pageIndex, setPageIndex] = useState(0)
  // Back to page 1 when the *settled* search changes, not per keystroke:
  // resetting on input would fetch the old search at page 1 first. Adjusted
  // during render (React's "storing information from previous renders"), so
  // no request goes out with the new query on the old page either.
  const [pagedQ, setPagedQ] = useState(q)
  if (pagedQ !== q) {
    setPagedQ(q)
    setPageIndex(0)
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
        snapshotStatus: filter === "all" ? undefined : filter,
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

  const columns = useMemo(() => getMarketplaceEntryColumns(), [])

  const changeSearch = (value: string) => setSearch(value)
  const changeFilter = (next: EntryFilter) => {
    setFilter(next)
    setPageIndex(0)
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
        serverPagination={{
          pageIndex,
          pageSize,
          total: entries.data.count,
          onPageChange: setPageIndex,
          onPageSizeChange: setPageSize,
        }}
        emptyState={
          <TableEmptyBlock text="No entries match.">
            <Button variant="link" size="sm" onClick={clearFilters}>
              Clear filters
            </Button>
          </TableEmptyBlock>
        }
        onRowClick={(entry) =>
          void navigate({
            to: "/admin/marketplace/plugin/$pluginId",
            params: { pluginId: entry.id },
          })
        }
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
          <div className="flex items-center gap-2">
            <FilterMenu
              options={FILTERS}
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
        </div>
      )}
      {body}
    </div>
  )
}
