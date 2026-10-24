import type { ColumnDef } from "@tanstack/react-table"
import { BadgeCheck, Plug } from "lucide-react"
import { useState } from "react"

import type { McpDirectoryEntryPublic } from "@/client"
import {
  formatRelativeTimestamp,
  RelativeTime,
} from "@/components/Common/RelativeTime"
import { StatusDot } from "@/components/Common/StatusDot"
import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { entryAccessLabel } from "@/utils/marketplace"
import {
  type McpEntryActions,
  McpEntryActionsMenu,
} from "./McpEntryActionsMenu"
import { useRowPending } from "./mcpDirectoryApi"
import { authLabel, sourceLabel, transportBadgeLabel } from "./mcpDirectoryCopy"

type Entry = McpDirectoryEntryPublic

/** The row's title button — also where focus lands after a menu write. */
export const entryTitleId = (entryId: string) => `mcp-entry-title-${entryId}`

/** 24 px tile: the server's icon, or `Plug` when it has none or it fails. */
function ServerTile({ iconUrl }: { iconUrl: string | null | undefined }) {
  const [failed, setFailed] = useState(false)
  return (
    <span className="flex h-6 w-6 shrink-0 items-center justify-center overflow-hidden rounded-md border bg-muted/50">
      {iconUrl && !failed ? (
        <img
          src={iconUrl}
          alt=""
          className="h-full w-full object-contain"
          onError={() => setFailed(true)}
        />
      ) : (
        <Plug className="h-3.5 w-3.5 text-muted-foreground" />
      )}
    </span>
  )
}

/**
 * The row's real control: keyboard and screen readers reach the Edit Sheet
 * here, the row click is the enhancement. Inert while a write on the row is
 * in flight, like the row itself.
 */
function EntryTitleButton({
  entry,
  onOpen,
}: {
  entry: Entry
  onOpen: () => void
}) {
  const pending = useRowPending(entry) !== null
  return (
    <button
      type="button"
      id={entryTitleId(entry.id)}
      className="truncate text-left text-sm font-medium hover:underline aria-disabled:cursor-default aria-disabled:no-underline"
      aria-disabled={pending || undefined}
      onClick={() => {
        if (!pending) onOpen()
      }}
    >
      {entry.title}
    </button>
  )
}

function AccessCell({ entry }: { entry: Entry }) {
  const { label, tone } = entryAccessLabel(entry.access)
  if (entry.access !== "banned") return <StatusDot tone={tone} label={label} />
  const changed = entry.access_changed_at
    ? `Changed ${formatRelativeTimestamp(entry.access_changed_at)}`
    : null
  const reason = [entry.access_reason?.trim() || null, changed]
    .filter(Boolean)
    .join(" · ")
  return <StatusDot tone={tone} label={label} reason={reason || null} />
}

/**
 * The MCP directory list (S4): Server · Transport · Auth · Access · Source ·
 * Updated · ⋯. Source and Updated drop below `xl`, so 1024 keeps four.
 */
export function getMcpDirectoryColumns(
  actions: McpEntryActions,
): ColumnDef<Entry>[] {
  return [
    {
      id: "server",
      header: "Server",
      meta: { className: "max-w-0" },
      cell: ({ row }) => {
        const entry = row.original
        return (
          <div className="flex min-w-0 items-center gap-3">
            <ServerTile iconUrl={entry.icon_url} />
            <div className="min-w-0">
              <div className="flex min-w-0 items-center gap-1.5">
                <EntryTitleButton
                  entry={entry}
                  onOpen={() => actions.onEdit(entry)}
                />
                {entry.verified && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="flex shrink-0">
                        <BadgeCheck className="h-3.5 w-3.5 text-primary" />
                        <span className="sr-only">Verified publisher</span>
                      </span>
                    </TooltipTrigger>
                    <TooltipContent side="top" className="text-xs">
                      Verified publisher
                    </TooltipContent>
                  </Tooltip>
                )}
              </div>
              <p className="truncate font-mono text-xs text-muted-foreground">
                {entry.name}
              </p>
            </div>
          </div>
        )
      },
    },
    {
      id: "transport",
      header: "Transport",
      meta: { className: "w-32" },
      cell: ({ row }) => (
        <Badge variant="outline" className="h-5">
          {transportBadgeLabel(row.original)}
        </Badge>
      ),
    },
    {
      id: "auth",
      header: "Auth",
      meta: { className: "w-32" },
      cell: ({ row }) => (
        <span className="text-sm">{authLabel(row.original.auth)}</span>
      ),
    },
    {
      id: "access",
      header: "Access",
      meta: { className: "w-28" },
      cell: ({ row }) => <AccessCell entry={row.original} />,
    },
    {
      id: "source",
      header: "Source",
      meta: { className: "hidden xl:table-cell w-28" },
      cell: ({ row }) => (
        <span className="text-sm text-muted-foreground">
          {sourceLabel(row.original.source)}
        </span>
      ),
    },
    {
      id: "updated",
      header: "Updated",
      meta: { className: "hidden xl:table-cell w-36" },
      cell: ({ row }) => (
        <RelativeTime
          timestamp={row.original.updated_at}
          showTooltip
          className="text-sm text-muted-foreground"
        />
      ),
    },
    {
      id: "actions",
      header: () => <span className="sr-only">Actions</span>,
      meta: { className: "w-12" },
      cell: ({ row }) => (
        <div className="flex justify-end">
          <McpEntryActionsMenu entry={row.original} actions={actions} />
        </div>
      ),
    },
  ]
}
