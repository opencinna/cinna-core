import type { ColumnDef } from "@tanstack/react-table"

import type { UserPublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import {
  INVITATION_STATUS_ACCEPTED,
  INVITATION_STATUS_EXPIRED,
  INVITATION_STATUS_PENDING,
  INVITATION_STATUS_REVOKED,
  isUnknownInvitationStatus,
  knownInvitationStatus,
} from "@/utils/invitationStatus"
import { UserActionsMenu } from "./UserActionsMenu"

export type UserTableData = UserPublic & {
  isCurrentUser: boolean
}

/**
 * The account's state in one line, from `is_active` and `invitation_status`.
 *
 * `is_active` is asked first on purpose: a deactivated account cannot be used
 * *or* accepted (the accept endpoint refuses a deactivated invitee), so
 * "Inactive" is the fact the admin has to deal with before the invitation's
 * own state matters.
 *
 * The invitation switch has no catch-all label, and an unrecognised value is
 * intercepted *before* it: a status this build has never heard of renders
 * nothing at all. Folding it into the "Active" arm would be the exact failure
 * the rule guards against — this same cell shows a pending invite on an
 * active account as "Invited", so "Active" is a positive claim that the
 * account has been claimed, not a neutral fallback.
 */
function UserStatusCell({ user }: { user: UserTableData }) {
  const status = knownInvitationStatus(user.invitation_status)

  if (!user.is_active) {
    return <StatusLine tone="muted" label="Inactive" />
  }

  if (isUnknownInvitationStatus(user.invitation_status)) {
    // The server knows something this build does not. Say nothing.
    return null
  }

  switch (status) {
    case INVITATION_STATUS_PENDING:
      return <StatusLine tone="pending" label="Invited" />
    case INVITATION_STATUS_EXPIRED:
      return <StatusLine tone="muted" label="Expired" />
    case INVITATION_STATUS_REVOKED:
      return <StatusLine tone="muted" label="Revoked" />
    case INVITATION_STATUS_ACCEPTED:
    case null:
      // Accepted, or never invited. Unrecognised values never reach here —
      // they were intercepted above.
      return <StatusLine tone="active" label="Active" />
  }
}

const DOT_TONES = {
  active: "bg-green-500",
  pending: "bg-amber-500",
  muted: "bg-gray-400",
} as const

function StatusLine({
  tone,
  label,
}: {
  tone: keyof typeof DOT_TONES
  label: string
}) {
  return (
    <div className="flex items-center gap-2">
      <span className={cn("size-2 rounded-full", DOT_TONES[tone])} />
      <span className={tone === "active" ? "" : "text-muted-foreground"}>
        {label}
      </span>
    </div>
  )
}

export const columns: ColumnDef<UserTableData>[] = [
  {
    accessorKey: "full_name",
    header: "Full Name",
    cell: ({ row }) => {
      const fullName = row.original.full_name
      return (
        <div className="flex items-center gap-2">
          <span
            className={cn("font-medium", !fullName && "text-muted-foreground")}
          >
            {fullName || "N/A"}
          </span>
          {row.original.isCurrentUser && (
            <Badge variant="outline" className="text-xs">
              You
            </Badge>
          )}
        </div>
      )
    },
  },
  {
    accessorKey: "email",
    header: "Email",
    cell: ({ row }) => (
      <span className="text-muted-foreground">{row.original.email}</span>
    ),
  },
  {
    accessorKey: "role",
    header: "Role",
    cell: ({ row }) => {
      const role = (row.original.role ?? "agent-user") as string
      const label =
        role === "admin"
          ? "Admin"
          : role === "agent-developer"
            ? "Agent Developer"
            : "Agent User"
      const variant =
        role === "admin"
          ? "default"
          : role === "agent-developer"
            ? "secondary"
            : "outline"
      return <Badge variant={variant}>{label}</Badge>
    },
  },
  {
    accessorKey: "is_active",
    header: "Status",
    cell: ({ row }) => <UserStatusCell user={row.original} />,
  },
  {
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    cell: ({ row }) => (
      <div className="flex justify-end">
        <UserActionsMenu user={row.original} />
      </div>
    ),
  },
]
