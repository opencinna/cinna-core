import { createFileRoute, redirect } from "@tanstack/react-router"
import { useEffect, useState } from "react"

import { UserGroupFormDialog } from "@/components/Admin/UserGroups/UserGroupFormDialog"
import { UserGroupsTable } from "@/components/Admin/UserGroups/UserGroupsTable"
import { Button } from "@/components/ui/button"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/admin/user-groups")({
  component: AdminUserGroups,
  head: () => ({
    meta: [{ title: `User Groups - Admin - ${APP_NAME}` }],
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

/** S2 — the admin's list of user groups (P9 Manage-list route). */
function AdminUserGroups() {
  const { setHeaderContent } = usePageHeader()
  const { user } = useAuth()
  const [createOpen, setCreateOpen] = useState(false)

  useEffect(() => {
    setHeaderContent(
      <>
        <div className="min-w-0">
          <h1 className="truncate text-lg font-semibold">User Groups</h1>
          <p className="text-xs text-muted-foreground">
            Share with many people in one step
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>Create group</Button>
      </>,
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
        <UserGroupsTable onCreate={() => setCreateOpen(true)} />
      </div>
      <UserGroupFormDialog open={createOpen} onOpenChange={setCreateOpen} />
    </div>
  )
}
