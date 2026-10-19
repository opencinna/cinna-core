import { useIsMutating } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { Unlink } from "lucide-react"
import type { ReactNode } from "react"

import type { UserGroupSharePublic } from "@/client"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  shareLevelLabel,
  shareResourceName,
  shareTypeLabel,
} from "@/utils/userGroupShareTypes"
import { revokeUserGroupShareMutationKey } from "./RevokeUserGroupShareDialog"
import { RowPendingSpinner } from "./RowPendingSpinner"

const NAME_CLASS =
  "block truncate rounded-sm text-sm font-medium outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
const LINK_CLASS = `${NAME_CLASS} hover:underline`

/**
 * The resource's own page, where a superuser can open it: catalog items and
 * knowledge sources. A credential's page decrypts its value on open and an
 * AI credential, artifact or producer agent (API scopes) has no page a
 * superuser can open for someone else's row, so those names stay plain text.
 */
function ResourceName({ share }: { share: UserGroupSharePublic }) {
  const name = shareResourceName(share)
  switch (share.resource_type) {
    case "agent_bundle":
    case "desktop_agent":
      return (
        <Link
          to="/catalog/items/$uuid"
          params={{ uuid: share.resource_id }}
          className={LINK_CLASS}
        >
          {name}
        </Link>
      )
    case "skill_package":
      return (
        <Link
          to="/catalog/skills/$packageId"
          params={{ packageId: share.resource_id }}
          className={LINK_CLASS}
        >
          {name}
        </Link>
      )
    case "knowledge_source":
      return (
        <Link
          to="/knowledge-source/$sourceId"
          params={{ sourceId: share.resource_id }}
          className={LINK_CLASS}
        >
          {name}
        </Link>
      )
    default:
      return (
        // Focusable, so the owner tooltip is reachable from the keyboard.
        // biome-ignore lint/a11y/noNoninteractiveTabindex: tooltip trigger
        <span tabIndex={0} className={NAME_CLASS}>
          {name}
        </span>
      )
  }
}

function NameCell({ share }: { share: UserGroupSharePublic }) {
  const owner = share.owner?.display_name
  const name = <ResourceName share={share} />
  if (!owner) return <div className="min-w-0">{name}</div>
  return (
    <div className="min-w-0">
      <Tooltip>
        <TooltipTrigger asChild>{name}</TooltipTrigger>
        <TooltipContent side="top" className="max-w-sm break-words text-xs">
          Owned by {owner}
        </TooltipContent>
      </Tooltip>
    </div>
  )
}

function RevokeCell({
  groupId,
  groupName,
  share,
  onRevoke,
}: {
  groupId: string
  groupName: string
  share: UserGroupSharePublic
  onRevoke: (share: UserGroupSharePublic) => void
}) {
  const revoking =
    useIsMutating({
      mutationKey: revokeUserGroupShareMutationKey(groupId, share),
    }) > 0
  if (revoking) {
    return (
      <RowPendingSpinner
        label="Revoking…"
        srLabel={`Revoking ${groupName}'s access to ${shareResourceName(share)}…`}
      />
    )
  }
  // The row's only action, and a destructive one: inline, hover-revealed.
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100"
          aria-label={`Revoke ${groupName}'s access to ${shareResourceName(share)}`}
          onClick={() => onRevoke(share)}
        >
          <Unlink className="h-3.5 w-3.5" />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        Revoke
      </TooltipContent>
    </Tooltip>
  )
}

function Muted({ children }: { children: ReactNode }) {
  return (
    <span className="block truncate text-sm text-muted-foreground">
      {children}
    </span>
  )
}

/** S5b — the "Shared with this group" tab's columns. */
export function getShareColumns({
  groupId,
  groupName,
  onRevoke,
}: {
  groupId: string
  groupName: string
  onRevoke: (share: UserGroupSharePublic) => void
}): ColumnDef<UserGroupSharePublic>[] {
  return [
    {
      id: "type",
      header: "Type",
      cell: ({ row }) => (
        <Badge variant="secondary" className="h-5 whitespace-nowrap">
          {shareTypeLabel(row.original.resource_type)}
        </Badge>
      ),
      meta: { className: "w-36" },
    },
    {
      id: "name",
      header: "Name",
      cell: ({ row }) => <NameCell share={row.original} />,
      meta: { className: "max-w-0" },
    },
    {
      id: "level",
      header: "Level",
      cell: ({ row }) => {
        const label = shareLevelLabel(row.original)
        if (row.original.resource_type !== "agent_api_scopes") {
          return <span className="block truncate text-sm">{label}</span>
        }
        // An API scope grant's level is its scope names, which can outrun the
        // column: truncated, with the whole list in a tooltip.
        return (
          <Tooltip>
            {/* The trigger's own button keeps the full list reachable from
                the keyboard. */}
            <TooltipTrigger className="block w-full truncate rounded-sm text-left text-sm outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50">
              {label}
            </TooltipTrigger>
            <TooltipContent side="top" className="max-w-sm break-words text-xs">
              {label}
            </TooltipContent>
          </Tooltip>
        )
      },
      meta: { className: "w-52" },
    },
    {
      id: "granted_by",
      header: "Granted by",
      // An unknown granter renders nothing (§2 Absent facts).
      cell: ({ row }) =>
        row.original.granted_by?.display_name ? (
          <Muted>{row.original.granted_by.display_name}</Muted>
        ) : null,
      meta: { className: "w-36" },
    },
    {
      id: "granted",
      header: "Granted",
      cell: ({ row }) => (
        <RelativeTime
          timestamp={row.original.granted_at}
          className="text-sm whitespace-nowrap text-muted-foreground"
          showTooltip
        />
      ),
      meta: { className: "hidden xl:table-cell w-36" },
    },
    {
      id: "actions",
      header: () => <span className="sr-only">Actions</span>,
      cell: ({ row }) => (
        <div className="flex justify-end">
          <RevokeCell
            groupId={groupId}
            groupName={groupName}
            share={row.original}
            onRevoke={onRevoke}
          />
        </div>
      ),
      meta: { className: "w-12" },
    },
  ]
}
