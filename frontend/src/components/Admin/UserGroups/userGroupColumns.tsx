import { useIsMutating } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { ArrowRight, Pencil, Trash2 } from "lucide-react"

import type { UserGroupPublic } from "@/client"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import { pluralize } from "@/utils/pluralize"
import { deleteUserGroupMutationKey } from "./DeleteUserGroupDialog"
import { RowPendingSpinner } from "./RowPendingSpinner"

function NameCell({ group }: { group: UserGroupPublic }) {
  return (
    <div className="min-w-0">
      {/* The link is the row's accessible target; the row's own click handler
          only widens the hit area. */}
      <Link
        to="/admin/user-group/$groupId"
        params={{ groupId: group.id }}
        className="block truncate rounded-sm text-sm font-medium outline-none hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/50"
      >
        {group.name}
      </Link>
      {group.description && (
        <p className="truncate text-xs text-muted-foreground">
          {group.description}
        </p>
      )}
    </div>
  )
}

function ActionsCell({
  group,
  onEdit,
  onDelete,
}: {
  group: UserGroupPublic
  onEdit: (group: UserGroupPublic) => void
  onDelete: (group: UserGroupPublic) => void
}) {
  const deleting =
    useIsMutating({ mutationKey: deleteUserGroupMutationKey(group.id) }) > 0
  if (deleting) {
    return (
      <RowPendingSpinner label="Deleting…" srLabel={`Deleting ${group.name}…`} />
    )
  }
  return (
    <RowActionsMenu label={`the group ${group.name}`}>
      <DropdownMenuItem asChild>
        <Link to="/admin/user-group/$groupId" params={{ groupId: group.id }}>
          <ArrowRight />
          Open
        </Link>
      </DropdownMenuItem>
      <DropdownMenuItem onSelect={() => onEdit(group)}>
        <Pencil />
        Edit
      </DropdownMenuItem>
      <DropdownMenuSeparator />
      <DropdownMenuItem variant="destructive" onSelect={() => onDelete(group)}>
        <Trash2 />
        Delete group
      </DropdownMenuItem>
    </RowActionsMenu>
  )
}

/** S2 — the User Groups list's columns. */
export function getUserGroupColumns({
  onEdit,
  onDelete,
}: {
  onEdit: (group: UserGroupPublic) => void
  onDelete: (group: UserGroupPublic) => void
}): ColumnDef<UserGroupPublic>[] {
  return [
    {
      id: "name",
      header: "Name",
      cell: ({ row }) => <NameCell group={row.original} />,
      meta: { className: "max-w-0" },
    },
    {
      id: "members",
      header: "Members",
      cell: ({ row }) => (
        <span className="text-sm whitespace-nowrap">
          {/* Active members only (the server's count). */}
          {row.original.member_count ?? 0} active
        </span>
      ),
      meta: { className: "w-32" },
    },
    {
      id: "shared",
      header: "Shared",
      // Nothing shared renders nothing (§2 Absent facts).
      cell: ({ row }) => {
        const count = row.original.share_count ?? 0
        return count > 0 ? (
          <span className="text-sm whitespace-nowrap">
            {pluralize(count, "item", "items")}
          </span>
        ) : null
      },
      meta: { className: "w-28" },
    },
    {
      id: "created",
      header: "Created",
      cell: ({ row }) => (
        <RelativeTime
          timestamp={row.original.created_at}
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
          <ActionsCell
            group={row.original}
            onEdit={onEdit}
            onDelete={onDelete}
          />
        </div>
      ),
      meta: { className: "w-12" },
    },
  ]
}
