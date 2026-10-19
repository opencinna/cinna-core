import { useIsMutating } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { UserMinus } from "lucide-react"

import type { UserGroupMemberPublic } from "@/client"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import {
  memberDisplayName,
  removeUserGroupMemberMutationKey,
} from "./RemoveUserGroupMemberDialog"
import { RowPendingSpinner } from "./RowPendingSpinner"

function RemoveCell({
  groupId,
  groupName,
  member,
  onRemove,
}: {
  groupId: string
  groupName: string
  member: UserGroupMemberPublic
  onRemove: (member: UserGroupMemberPublic) => void
}) {
  const name = memberDisplayName(member)
  const removing =
    useIsMutating({
      mutationKey: removeUserGroupMemberMutationKey(groupId, member.user_id),
    }) > 0
  if (removing) {
    return (
      <RowPendingSpinner
        label="Removing…"
        srLabel={`Removing ${name} from ${groupName}…`}
      />
    )
  }
  // The row's only action, and a destructive one: inline, hover-revealed
  // (§2 Row actions, single-action case).
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100"
          aria-label={`Remove ${name} from ${groupName}`}
          onClick={() => onRemove(member)}
        >
          <UserMinus className="h-3.5 w-3.5" />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        Remove from group
      </TooltipContent>
    </Tooltip>
  )
}

/** S5a — the Members tab's columns. */
export function getMemberColumns({
  groupId,
  groupName,
  onRemove,
}: {
  groupId: string
  groupName: string
  onRemove: (member: UserGroupMemberPublic) => void
}): ColumnDef<UserGroupMemberPublic>[] {
  return [
    {
      id: "name",
      header: "Name",
      cell: ({ row }) => {
        const member = row.original
        return (
          <div className="flex min-w-0 items-center gap-2">
            <span
              className={cn(
                "truncate text-sm font-medium",
                !member.is_active && "text-muted-foreground",
              )}
            >
              {memberDisplayName(member)}
            </span>
            {/* An active member renders nothing here (§2 Absent facts). */}
            {!member.is_active && (
              <Badge variant="outline" className="h-5 shrink-0">
                Inactive
              </Badge>
            )}
          </div>
        )
      },
      meta: { className: "w-[40%] max-w-0" },
    },
    {
      id: "email",
      header: "Email",
      // When the name cell already fell back to the email, it is not repeated.
      cell: ({ row }) =>
        row.original.full_name ? (
          <span className="block truncate text-sm text-muted-foreground">
            {row.original.email}
          </span>
        ) : null,
      meta: { className: "max-w-0" },
    },
    {
      id: "added",
      header: "Added",
      cell: ({ row }) => (
        <RelativeTime
          timestamp={row.original.added_at}
          className="text-sm whitespace-nowrap text-muted-foreground"
          showTooltip
        />
      ),
      meta: { className: "hidden lg:table-cell w-40" },
    },
    {
      id: "actions",
      header: () => <span className="sr-only">Actions</span>,
      cell: ({ row }) => (
        <div className="flex justify-end">
          <RemoveCell
            groupId={groupId}
            groupName={groupName}
            member={row.original}
            onRemove={onRemove}
          />
        </div>
      ),
      meta: { className: "w-12" },
    },
  ]
}
