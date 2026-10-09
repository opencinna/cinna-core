import { createFileRoute, redirect } from "@tanstack/react-router"
import { useEffect } from "react"

import { TeamLibraryTable } from "@/components/Admin/TeamLibrary/TeamLibraryTable"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/admin/team-library")({
  component: AdminTeamLibrary,
  head: () => ({
    meta: [
      {
        title: `Team Library - Admin - ${APP_NAME}`,
      },
    ],
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

function AdminTeamLibrary() {
  const { setHeaderContent } = usePageHeader()
  const { user } = useAuth()

  useEffect(() => {
    // No primary button: agents join the team library from Cinna Desktop or
    // the CLI, so there is no web create action for the header to carry.
    setHeaderContent(
      <div className="min-w-0">
        <h1 className="text-lg font-semibold truncate">Team Library</h1>
        <p className="text-xs text-muted-foreground">
          Agents shared with the team, who maintains them and who can get them
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
        <TeamLibraryTable />
      </div>
    </div>
  )
}
