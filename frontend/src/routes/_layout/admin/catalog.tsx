import { createFileRoute, redirect } from "@tanstack/react-router"
import { useEffect } from "react"

import { AdminCatalogTable } from "@/components/Admin/Catalog/AdminCatalogTable"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/admin/catalog")({
  component: AdminCatalog,
  head: () => ({
    meta: [{ title: `Catalog - Admin - ${APP_NAME}` }],
  }),
  beforeLoad: async ({ context }) => {
    if (!isLoggedIn()) {
      throw redirect({ to: "/login" })
    }
    // context.user is populated by the _layout auth guard.
    const user = (context as { user?: { is_superuser?: boolean } })?.user
    if (user && !user.is_superuser) {
      throw redirect({ to: "/" })
    }
  },
})

function AdminCatalog() {
  const { setHeaderContent } = usePageHeader()
  const { user } = useAuth()

  useEffect(() => {
    // No primary button: nothing is created in the catalog from the web.
    setHeaderContent(
      <div className="min-w-0">
        <h1 className="truncate text-lg font-semibold">Catalog</h1>
        <p className="text-xs text-muted-foreground">
          Every agent and skill in the catalog, who maintains it and who can get
          it.
        </p>
      </div>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  // Edge-case guard for a non-superuser that slipped past beforeLoad.
  if (user && !user.is_superuser) {
    return (
      <div className="p-6 text-center text-muted-foreground">
        You do not have permission to view this page.
      </div>
    )
  }

  return (
    <div className="overflow-y-auto p-6 md:p-8">
      <div className="mx-auto max-w-7xl">
        <AdminCatalogTable />
      </div>
    </div>
  )
}
