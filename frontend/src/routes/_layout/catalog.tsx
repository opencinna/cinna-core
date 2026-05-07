import { useQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Store } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import { CatalogService } from "@/client"
import { CatalogFilters, type CatalogFilter } from "@/components/Catalog/CatalogFilters"
import { CatalogGrid } from "@/components/Catalog/CatalogGrid"
import PendingItems from "@/components/Pending/PendingItems"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/catalog")({
  component: Catalog,
  head: () => ({
    meta: [{ title: `Catalog - ${APP_NAME}` }],
  }),
})

function Catalog() {
  const { setHeaderContent } = usePageHeader()
  const [filter, setFilter] = useState<CatalogFilter>("all")

  useEffect(() => {
    setHeaderContent(
      <div className="min-w-0">
        <h1 className="text-lg font-semibold truncate">Catalog</h1>
        <p className="text-xs text-muted-foreground truncate">
          Install agent bundles published on this instance
        </p>
      </div>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  const { data, isLoading, error } = useQuery({
    queryKey: ["catalog"],
    queryFn: () => CatalogService.listCatalog(),
  })

  const entries = data?.data ?? []

  const filtered = useMemo(() => {
    switch (filter) {
      case "public":
        return entries.filter((e) => e.visibility === "public")
      case "shared":
        return entries.filter((e) => e.visibility === "users")
      case "installed":
        return entries.filter((e) => e.is_installed)
      default:
        return entries
    }
  }, [entries, filter])

  if (isLoading) {
    return <PendingItems />
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">
          Error loading catalog: {(error as Error).message}
        </p>
      </div>
    )
  }

  return (
    <div className="p-6 md:p-8 overflow-y-auto space-y-6">
      <div className="mx-auto max-w-7xl space-y-4">
        <CatalogFilters value={filter} onChange={setFilter} />

        {filtered.length === 0 ? (
          <div className="flex flex-col items-center justify-center text-center py-16">
            <div className="rounded-full bg-muted p-4 mb-4">
              <Store className="h-8 w-8 text-muted-foreground" />
            </div>
            <h3 className="text-lg font-semibold">
              {entries.length === 0
                ? "No bundles available"
                : "No bundles match this filter"}
            </h3>
            <p className="text-muted-foreground max-w-md">
              {entries.length === 0
                ? "Ask an admin to publish or grant you access to a bundle to see it here."
                : "Try a different filter — there are bundles in the catalog you don't see in this view."}
            </p>
          </div>
        ) : (
          <CatalogGrid entries={filtered} />
        )}
      </div>
    </div>
  )
}
