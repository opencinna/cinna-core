import { useQuery } from "@tanstack/react-query"
import { Link, useNavigate } from "@tanstack/react-router"
import { Search } from "lucide-react"
import { type ReactNode, useMemo, useState } from "react"

import { type CatalogItem, CatalogService } from "@/client"
import { CatalogKindTabs } from "@/components/Catalog/CatalogKindTabs"
import { useCatalogItemActions } from "@/components/Catalog/useCatalogItemActions"
import { DataTable } from "@/components/Common/DataTable"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import {
  type CatalogKindParam,
  catalogDetailTarget,
  catalogItemsQueryKey,
  matchesCatalogKind,
} from "@/utils/catalogItems"
import { adminCatalogColumns } from "./adminCatalogColumns"

function matchesQuery(item: CatalogItem, q: string): boolean {
  if (!q) return true
  const haystack = [
    item.display_name,
    item.name,
    item.id,
    ...(item.maintainers ?? []).map((m) => m.display_name),
  ]
  return haystack.some((s) => (s || "").toLowerCase().includes(q))
}

/**
 * S4 body: every item in the catalog (`scope=all`), filtered by kind and a
 * client-side search, with the People dialog, the delete confirm and the
 * delist confirm owned here so a row's menu item only sets a target.
 */
export function AdminCatalogTable() {
  const navigate = useNavigate()
  const [kind, setKind] = useState<CatalogKindParam>("all")
  const [query, setQuery] = useState("")
  const actions = useCatalogItemActions()

  const itemsQuery = useQuery({
    queryKey: catalogItemsQueryKey("all"),
    queryFn: () => CatalogService.listCatalogItems({ scope: "all" }),
  })

  const q = query.trim().toLowerCase()
  const rows = useMemo(
    () =>
      (itemsQuery.data?.data ?? []).filter(
        (item) => matchesCatalogKind(item, kind) && matchesQuery(item, q),
      ),
    [itemsQuery.data, kind, q],
  )

  const { pendingLabel, openPeople, setListed, openDelete } = actions
  const columns = useMemo(
    () =>
      adminCatalogColumns({
        onOpen: (item) => navigate(catalogDetailTarget(item)),
        onPeople: openPeople,
        onSetListed: setListed,
        onDelete: openDelete,
        pendingLabel,
      }),
    [navigate, openPeople, setListed, openDelete, pendingLabel],
  )

  const toolbar = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <CatalogKindTabs value={kind} onChange={setKind} />
      {/* Narrow enough to share the row with the kind control at 1024. */}
      <div className="relative w-full sm:w-40 xl:w-64">
        <Search className="pointer-events-none absolute top-1/2 left-2.5 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search"
          aria-label="Search by name, id or maintainer"
          className="pl-8"
        />
      </div>
    </div>
  )

  let body: ReactNode
  if (itemsQuery.isError && itemsQuery.data === undefined) {
    body = (
      <QueryErrorAlert
        error={itemsQuery.error}
        fallback="Couldn't load the catalog"
        onRetry={() => itemsQuery.refetch()}
      />
    )
  } else if (itemsQuery.data === undefined) {
    body = (
      <div className="space-y-2">
        {Array.from({ length: 5 }, (_, i) => (
          <Skeleton key={i} className="h-10 w-full rounded-md" />
        ))}
      </div>
    )
  } else if (itemsQuery.data.data.length === 0) {
    // In place of the table rather than inside it: a header row over nothing
    // reads as a table that failed to fill.
    body = (
      <div className="flex flex-col items-center justify-center rounded-md border py-16 text-center">
        <p className="max-w-md text-sm text-muted-foreground">
          Nothing is in the catalog yet. Agents and skills appear here when
          someone shares them.{" "}
          <Link to="/catalog" className="text-primary hover:underline">
            Open the catalog
          </Link>
        </p>
      </div>
    )
  } else {
    body = (
      <DataTable
        columns={columns}
        data={rows}
        getRowId={(item) => item.uuid}
        emptyState={
          <span className="text-sm text-muted-foreground">
            {q ? `No items match "${query.trim()}".` : "No items of this kind."}
          </span>
        }
      />
    )
  }

  return (
    <div className="space-y-4">
      {toolbar}
      {body}
      {actions.dialogs}
    </div>
  )
}
