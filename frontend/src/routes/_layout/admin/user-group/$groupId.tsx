import { useQuery } from "@tanstack/react-query"
import { createFileRoute, Link, redirect } from "@tanstack/react-router"
import { ArrowLeft, Pencil, Trash2 } from "lucide-react"
import { useEffect, useState } from "react"

import { AdminUserGroupsService, type UserGroupPublic } from "@/client"
import { ApiError } from "@/client/core/ApiError"
import { DeleteUserGroupDialog } from "@/components/Admin/UserGroups/DeleteUserGroupDialog"
import { UserGroupFormDialog } from "@/components/Admin/UserGroups/UserGroupFormDialog"
import { UserGroupMembersTab } from "@/components/Admin/UserGroups/UserGroupMembersTab"
import { UserGroupSharesTab } from "@/components/Admin/UserGroups/UserGroupSharesTab"
import { userGroupKeys } from "@/components/Admin/UserGroups/userGroupQueries"
import { HashTabs } from "@/components/Common/HashTabs"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import PendingItems from "@/components/Pending/PendingItems"
import { Button } from "@/components/ui/button"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import { useNavigationHistory } from "@/hooks/useNavigationHistory"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/admin/user-group/$groupId")({
  component: UserGroupDetailPage,
  head: () => ({
    meta: [{ title: `User Group - Admin - ${APP_NAME}` }],
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

function DetailHeader({
  group,
  onBack,
  onEdit,
  onDelete,
}: {
  group: UserGroupPublic | undefined
  onBack: () => void
  onEdit: () => void
  onDelete: () => void
}) {
  return (
    <>
      <div className="flex min-w-0 items-center gap-3">
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="sm"
              onClick={onBack}
              className="shrink-0"
              aria-label="Back to groups"
            >
              <ArrowLeft className="h-4 w-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom" className="text-xs">
            Back to groups
          </TooltipContent>
        </Tooltip>
        <div className="min-w-0">
          <h1 className="truncate text-base font-semibold">
            {group?.name || "User group"}
          </h1>
          <p className="truncate text-xs text-muted-foreground">
            {group?.description || "User group"}
          </p>
        </div>
      </div>
      {group && (
        <RowActionsMenu label={`the group ${group.name}`}>
          <DropdownMenuItem onSelect={onEdit}>
            <Pencil />
            Edit
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem variant="destructive" onSelect={onDelete}>
            <Trash2 />
            Delete group
          </DropdownMenuItem>
        </RowActionsMenu>
      )}
    </>
  )
}

/** S5 — one group: its members and what is shared with it, as two tabs. */
function UserGroupDetailPage() {
  const { groupId } = Route.useParams()
  const { user } = useAuth()
  const { setHeaderContent } = usePageHeader()
  const { goBack } = useNavigationHistory()
  const [editOpen, setEditOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)

  const groupQuery = useQuery({
    queryKey: userGroupKeys.detail(groupId),
    queryFn: () => AdminUserGroupsService.getUserGroup({ groupId }),
    retry: (count, error) =>
      !(error instanceof ApiError && error.status === 404) && count < 2,
  })
  const group = groupQuery.data

  useEffect(() => {
    setHeaderContent(
      <DetailHeader
        group={group}
        onBack={() => goBack("/admin/user-groups")}
        onEdit={() => setEditOpen(true)}
        onDelete={() => setDeleteOpen(true)}
      />,
    )
    return () => setHeaderContent(null)
  }, [group, setHeaderContent, goBack])

  // Edge-case guard for a non-superuser that slipped past beforeLoad.
  if (user && !user.is_superuser) {
    return (
      <div className="p-6 text-center text-muted-foreground">
        You do not have permission to view this page.
      </div>
    )
  }

  let body: React.ReactNode
  if (groupQuery.isError && group === undefined) {
    const gone =
      groupQuery.error instanceof ApiError && groupQuery.error.status === 404
    body = gone ? (
      <div className="flex flex-col items-center gap-3 py-12 text-center">
        <p className="text-sm text-muted-foreground">
          This group no longer exists.
        </p>
        <Button variant="link" size="sm" asChild>
          <Link to="/admin/user-groups">Back to User Groups</Link>
        </Button>
      </div>
    ) : (
      <QueryErrorAlert
        error={groupQuery.error}
        fallback="Couldn't load this group."
        onRetry={() => groupQuery.refetch()}
      />
    )
  } else if (group === undefined) {
    body = <PendingItems />
  } else {
    body = (
      <HashTabs
        defaultTab="members"
        tabs={[
          {
            value: "members",
            // `member_count` counts active members only; the table also lists
            // inactive ones, so the title says which rows it counts.
            title: `Members (${group.member_count ?? 0} active)`,
            content: <UserGroupMembersTab group={group} />,
          },
          {
            value: "shares",
            title: `Shared with this group (${group.share_count ?? 0})`,
            content: <UserGroupSharesTab group={group} />,
          },
        ]}
      />
    )
  }

  return (
    <div className="overflow-y-auto p-6 md:p-8">
      <div className="mx-auto max-w-7xl">{body}</div>
      {group && (
        <>
          {editOpen && (
            <UserGroupFormDialog
              open
              onOpenChange={setEditOpen}
              group={group}
            />
          )}
          {deleteOpen && (
            <DeleteUserGroupDialog
              group={group}
              open
              onOpenChange={setDeleteOpen}
              returnToList
            />
          )}
        </>
      )}
    </div>
  )
}
