import { useSuspenseQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Suspense, useEffect } from "react"

import { LlmPluginsService } from "@/client"
import AddMarketplace from "@/components/Admin/AddMarketplace"
import { marketplaceColumns } from "@/components/Admin/marketplaceColumns"
import { DataTable } from "@/components/Common/DataTable"
import PendingItems from "@/components/Pending/PendingItems"
import { usePageHeader } from "@/routes/_layout"

function getMarketplacesQueryOptions() {
  return {
    queryFn: () => LlmPluginsService.listMarketplaces({ includePublic: true }),
    queryKey: ["marketplaces"],
  }
}

export const Route = createFileRoute("/_layout/admin/marketplaces")({
  component: AdminMarketplaces,
  head: () => ({
    meta: [
      {
        title: "Plugin Marketplaces - Admin - Workflow Runner",
      },
    ],
  }),
})

function MarketplacesTableContent() {
  const { data: marketplaces } = useSuspenseQuery(getMarketplacesQueryOptions())

  return <DataTable columns={marketplaceColumns} data={marketplaces.data} />
}

function MarketplacesTable() {
  return (
    <Suspense fallback={<PendingItems />}>
      <MarketplacesTableContent />
    </Suspense>
  )
}

function AdminMarketplaces() {
  const { setHeaderContent } = usePageHeader()

  useEffect(() => {
    setHeaderContent(
      <>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">Plugin Marketplaces</h1>
          <p className="text-xs text-muted-foreground">
            Manage plugin repositories and sync plugins
          </p>
        </div>
        <AddMarketplace />
      </>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        <MarketplacesTable />
      </div>
    </div>
  )
}
