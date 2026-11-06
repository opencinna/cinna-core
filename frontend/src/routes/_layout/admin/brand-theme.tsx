import { createFileRoute, redirect } from "@tanstack/react-router"
import { useEffect } from "react"

import { BrandThemeAdmin } from "@/components/Admin/BrandTheme/BrandThemeAdmin"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/admin/brand-theme")({
  component: AdminBrandTheme,
  head: () => ({
    meta: [
      {
        title: `Brand Theme - Admin - ${APP_NAME}`,
      },
    ],
  }),
  beforeLoad: async ({ context }) => {
    if (!isLoggedIn()) {
      throw redirect({ to: "/login" })
    }
    // context.user is populated by the _layout auth guard.
    const user = (context as { user?: { is_superuser?: boolean } }).user
    if (user && !user.is_superuser) {
      throw redirect({ to: "/" })
    }
  },
})

function AdminBrandTheme() {
  const { setHeaderContent } = usePageHeader()
  const { user } = useAuth()

  useEffect(() => {
    setHeaderContent(
      <div className="min-w-0">
        <h1 className="text-lg font-semibold truncate">Brand Theme</h1>
        <p className="text-xs text-muted-foreground">
          How the sign-in pages and Cinna Desktop look
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
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        <BrandThemeAdmin />
      </div>
    </div>
  )
}
