import { useQuery } from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import type { LucideIcon } from "lucide-react"
import { type ReactNode, useEffect, useMemo } from "react"

import { CatalogService } from "@/client"
import { CatalogItemFilters } from "@/components/Catalog/CatalogItemFilters"
import {
  CATALOG_GRID_CLASS,
  CatalogItemGrid,
} from "@/components/Catalog/CatalogItemGrid"
import { CatalogKindTabs } from "@/components/Catalog/CatalogKindTabs"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"
import {
  CATALOG_KINDS,
  type CatalogFilter,
  type CatalogKindParam,
  catalogItemsQueryKey,
  matchesCatalogFilter,
  matchesCatalogKind,
  parseCatalogFilter,
  parseCatalogKind,
} from "@/utils/catalogItems"

interface CatalogSearch {
  kind?: Exclude<CatalogKindParam, "all">
  filter?: Exclude<CatalogFilter, "all">
}

export const Route = createFileRoute("/_layout/catalog/")({
  component: Catalog,
  // Both selections live in the URL, so a narrowed grid can be linked to and
  // Back returns to it. "all" is the absence of the param.
  validateSearch: (search: Record<string, unknown>): CatalogSearch => {
    const kind = parseCatalogKind(search.kind)
    const filter = parseCatalogFilter(search.filter)
    return {
      kind: kind === "all" ? undefined : kind,
      filter: filter === "all" ? undefined : filter,
    }
  },
  head: () => ({
    meta: [{ title: `Catalog - ${APP_NAME}` }],
  }),
})

function Catalog() {
  const { setHeaderContent } = usePageHeader()
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  const kind: CatalogKindParam = search.kind ?? "all"
  const filter: CatalogFilter = search.filter ?? "all"

  useEffect(() => {
    setHeaderContent(
      <div className="min-w-0">
        <h1 className="text-lg font-semibold truncate">Catalog</h1>
        <p className="text-xs text-muted-foreground truncate">
          Agents and skills shared on this instance
        </p>
      </div>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: catalogItemsQueryKey(),
    queryFn: () => CatalogService.listCatalogItems(),
  })

  // One fetch, narrowed in the client: switching kind or filter is instant and
  // the empty state can tell "nothing of this kind" from "nothing matches".
  const ofKind = useMemo(
    () => (data?.data ?? []).filter((item) => matchesCatalogKind(item, kind)),
    [data, kind],
  )
  const shown = useMemo(
    () => ofKind.filter((item) => matchesCatalogFilter(item, filter)),
    [ofKind, filter],
  )

  const setKind = (next: CatalogKindParam) =>
    navigate({
      search: (prev) => ({ ...prev, kind: next === "all" ? undefined : next }),
      replace: true,
    })
  const setFilter = (next: CatalogFilter) =>
    navigate({
      search: (prev) => ({
        ...prev,
        filter: next === "all" ? undefined : next,
      }),
      replace: true,
    })

  return (
    <div className="p-6 md:p-8 overflow-y-auto space-y-6">
      <div className="mx-auto max-w-7xl space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <CatalogKindTabs value={kind} onChange={setKind} />
          <CatalogItemFilters value={filter} onChange={setFilter} />
        </div>

        {/* Gated on there being no data: a failed background refetch keeps
            the grid; a first failed load is a failure, never "nothing yet". */}
        {isError && !data ? (
          <QueryErrorAlert
            error={error}
            fallback="Couldn't load the catalog"
            onRetry={() => refetch()}
          />
        ) : isLoading ? (
          <div className={CATALOG_GRID_CLASS}>
            {Array.from({ length: 8 }, (_, i) => (
              <Skeleton key={i} className="h-[240px] w-full rounded-xl" />
            ))}
          </div>
        ) : shown.length > 0 ? (
          <CatalogItemGrid items={shown} />
        ) : ofKind.length > 0 ? (
          <EmptyBlock>
            <p className="text-muted-foreground">
              Nothing matches this filter.
            </p>
            <Button variant="link" onClick={() => setFilter("all")}>
              Show everything
            </Button>
          </EmptyBlock>
        ) : (
          <KindEmptyState kind={kind} />
        )}
      </div>
    </div>
  )
}

function EmptyBlock({
  children,
  icon: Icon,
}: {
  children: ReactNode
  icon?: LucideIcon
}) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      {Icon && (
        <div className="mb-4 rounded-full bg-muted p-4">
          <Icon className="h-8 w-8 text-muted-foreground" />
        </div>
      )}
      {children}
    </div>
  )
}

/** Unfiltered and still empty: one sentence per kind, with the way to fill it. */
function KindEmptyState({ kind }: { kind: CatalogKindParam }) {
  const icon =
    kind === "all" ? CATALOG_KINDS.cloud.icon : CATALOG_KINDS[kind].icon
  let body: ReactNode
  switch (kind) {
    case "cloud":
      body = (
        <>
          Publish one from{" "}
          <Link to="/agents" className="text-primary hover:underline">
            an agent's Bundle tab
          </Link>
          .
        </>
      )
      break
    case "desktop":
      body = "Share one from Cinna Desktop."
      break
    case "skill":
      body = (
        <>
          Publish a skill from{" "}
          <Link to="/agents" className="text-primary hover:underline">
            an agent's Configuration tab
          </Link>{" "}
          to see it here.
        </>
      )
      break
    default:
      body = "Nothing is shared with you yet."
  }
  return (
    <EmptyBlock icon={icon}>
      <p className="max-w-md text-muted-foreground">{body}</p>
    </EmptyBlock>
  )
}
