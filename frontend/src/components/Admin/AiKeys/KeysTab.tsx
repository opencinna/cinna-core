import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { useEffect, useMemo, useState } from "react"

import {
  AdminAiKeysService,
  AdminAiProvidersService,
  type MembershipProvisioningStatus,
} from "@/client"
import { DataTable } from "@/components/Common/DataTable"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { MEMBERSHIP_STATUS_META } from "@/utils/keyProvisioning"
import { AI_PROVIDERS_QUERY_KEY } from "../LlmProviders/providerTypes"
import {
  type AiKeysFilters,
  aiKeysQueryKey,
  EMPTY_AI_KEYS_FILTERS,
  hasActiveFilters,
  keyRowId,
  pageHasKeyInFlight,
} from "./aiKeys"
import { keyColumns } from "./keyColumns"

const PAGE_SIZE = 25

/**
 * The loading state, shaped like the table it stands in for (§6).
 *
 * Deliberately not the shared `PendingItems`, which hard-codes `ID | Title |
 * Description | Actions` — the columns of the deleted `items` demo. A skeleton
 * that announces three columns this table does not have is a worse answer than
 * no skeleton at all.
 */
function KeysTableSkeleton() {
  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead>Key</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Type</TableHead>
            <TableHead>Source</TableHead>
            <TableHead>Created</TableHead>
            <TableHead className="w-[48px]">
              <span className="sr-only">Actions</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {Array.from({ length: 5 }).map((_, index) => (
            <TableRow key={index}>
              <TableCell>
                <Skeleton className="h-4 w-48" />
                <Skeleton className="mt-1 h-3 w-32" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-24" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-5 w-16" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-28" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-20" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-7 w-7 rounded-md" />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

/**
 * The statuses worth filtering by, in lifecycle order.
 *
 * Read off `MEMBERSHIP_STATUS_META` rather than listed here, so a seventh
 * status added on the server appears in the filter instead of being silently
 * unfilterable. `not_applicable` is excluded: it is what every *shared*
 * membership carries, and a shared row is filtered by kind, not by status —
 * the server excludes them from a status query for the same reason.
 */
const STATUS_OPTIONS = (
  Object.keys(MEMBERSHIP_STATUS_META) as MembershipProvisioningStatus[]
).filter((status) => status !== "not_applicable")

/**
 * Every API key in the fleet, one row each.
 *
 * The tab this replaces listed *records*: for a minted provider that was one
 * row hiding one key per member, rendered as an unbounded chip cell, paginated
 * by record so the count that grows was never the one being paged. Here the row
 * is the key, the filtering and paging happen on the server, and the payload no
 * longer grows with headcount.
 */
export function KeysTab({ active }: { active: boolean }) {
  const [filters, setFilters] = useState<AiKeysFilters>(EMPTY_AI_KEYS_FILTERS)
  const [search, setSearch] = useState("")
  const [pageIndex, setPageIndex] = useState(0)

  // Every filter change moves the list under the pager, so page 1 is the only
  // page still meaningful — and the reset happens *with* the change rather than
  // in an effect watching for it. An effect whose body reads none of its
  // dependencies is a lint error here and, more to the point, a second place
  // the two pieces of state can fall out of step.
  const applyFilters = (next: Partial<AiKeysFilters>) => {
    setFilters((prev) => ({ ...prev, ...next }))
    setPageIndex(0)
  }

  const clearFilters = () => {
    setSearch("")
    setFilters(EMPTY_AI_KEYS_FILTERS)
    setPageIndex(0)
  }

  // Debounced, because every keystroke is a request otherwise. The committed
  // value lives in `filters`, so the query key changes once per pause.
  useEffect(() => {
    const timer = setTimeout(() => {
      setFilters((prev) => (prev.q === search ? prev : { ...prev, q: search }))
      setPageIndex(0)
    }, 300)
    return () => clearTimeout(timer)
  }, [search])

  const { data: providers } = useQuery({
    queryKey: AI_PROVIDERS_QUERY_KEY,
    queryFn: () => AdminAiProvidersService.listAiProviders(),
    staleTime: 60_000,
    enabled: active,
  })

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: aiKeysQueryKey(filters, pageIndex, PAGE_SIZE),
    queryFn: () =>
      AdminAiKeysService.listAiKeys({
        q: filters.q.trim() || undefined,
        status:
          filters.status === "all"
            ? undefined
            : [filters.status as MembershipProvisioningStatus],
        kind: filters.kind === "all" ? undefined : filters.kind,
        providerId:
          filters.providerId === "all" ? undefined : filters.providerId,
        skip: pageIndex * PAGE_SIZE,
        limit: PAGE_SIZE,
      }),
    staleTime: 30_000,
    // A page turn is a new query key, so without this the table unmounts and
    // the skeleton takes its place for the length of a round trip — the list
    // jumps every time an admin pages through it. The previous page stays put
    // until the next one arrives.
    placeholderData: keepPreviousData,
    // A key being created is the one thing here that moves without an admin
    // doing anything, so the list follows it and stops when it settles. Scoped
    // to the rows on *this* page, and to this tab being the visible one.
    refetchInterval: (query) =>
      active && pageHasKeyInFlight(query.state.data?.data ?? [])
        ? 10_000
        : false,
  })

  const rows = useMemo(() => data?.data ?? [], [data])
  const filtered = hasActiveFilters(filters)

  const emptyState = filtered ? (
    <div className="space-y-2">
      <p>No keys match this search.</p>
      <Button variant="link" className="h-auto p-0" onClick={clearFilters}>
        Clear filters
      </Button>
    </div>
  ) : (
    <div className="space-y-2">
      <p>No keys yet.</p>
      <p className="text-xs">
        <Link
          to="/admin/ai-credentials"
          hash="providers"
          className="text-primary hover:underline"
        >
          Connect a provider
        </Link>{" "}
        to start issuing keys.
      </p>
    </div>
  )

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search by person or credential"
          className="min-w-[16rem] max-w-sm"
          aria-label="Search keys"
        />
        <Select
          value={filters.status}
          onValueChange={(value) =>
            applyFilters({ status: value as AiKeysFilters["status"] })
          }
        >
          <SelectTrigger className="w-[10rem]" aria-label="Filter by status">
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Any status</SelectItem>
            {STATUS_OPTIONS.map((status) => (
              <SelectItem key={status} value={status}>
                {MEMBERSHIP_STATUS_META[status].adminLabel}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={filters.kind}
          onValueChange={(value) =>
            applyFilters({ kind: value as AiKeysFilters["kind"] })
          }
        >
          <SelectTrigger className="w-[10rem]" aria-label="Filter by kind">
            <SelectValue placeholder="Kind" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Any kind</SelectItem>
            <SelectItem value="per_user">Per-user keys</SelectItem>
            <SelectItem value="shared">Shared keys</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={filters.providerId}
          onValueChange={(value) => applyFilters({ providerId: value })}
        >
          <SelectTrigger className="w-[12rem]" aria-label="Filter by provider">
            <SelectValue placeholder="Provider" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Any provider</SelectItem>
            {(providers ?? []).map((provider) => (
              <SelectItem key={provider.id} value={provider.id}>
                {provider.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {filtered && (
          <Button variant="ghost" size="sm" onClick={clearFilters}>
            Clear
          </Button>
        )}
      </div>

      {/* Error before "nothing yet", and gated on there being nothing to show:
          a failed background refetch must not replace a live table with an
          error panel, and a failed *first* read must not render as an empty
          list — an admin who reads "no keys yet" after a 500 concludes the
          company has none. */}
      {isError && data === undefined ? (
        <QueryErrorAlert
          error={error}
          fallback="Couldn't load the keys."
          onRetry={() => refetch()}
        />
      ) : isLoading || !data ? (
        <KeysTableSkeleton />
      ) : (
        <DataTable
          columns={keyColumns}
          data={rows}
          getRowId={keyRowId}
          emptyState={emptyState}
          serverPagination={{
            pageIndex,
            pageSize: PAGE_SIZE,
            total: data.count ?? 0,
            onPageChange: setPageIndex,
          }}
        />
      )}
    </div>
  )
}
