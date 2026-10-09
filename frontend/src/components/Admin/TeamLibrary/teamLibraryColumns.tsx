import type { ColumnDef } from "@tanstack/react-table"
import { Fragment } from "react"

import type { LibraryAgentAdmin } from "@/client"
import { RelativeTime } from "@/components/Common/RelativeTime"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { audienceFacts } from "@/utils/libraryAccess"
import { TeamLibraryRowActions } from "./TeamLibraryRowActions"

interface TeamLibraryColumnHandlers {
  onEditAccess: (agent: LibraryAgentAdmin) => void
  onRemove: (agent: LibraryAgentAdmin) => void
  /** The agent whose removal is in flight, if any. */
  removingId: string | null
}

const VISIBLE_MAINTAINERS = 2

function MaintainersCell({ agent }: { agent: LibraryAgentAdmin }) {
  const names = (agent.maintainers ?? []).map(
    (m) => m.display_name || "Unknown person",
  )
  if (names.length === 0) return null
  const shown = names.slice(0, VISIBLE_MAINTAINERS)
  const rest = names.length - shown.length
  // The tooltip lists every maintainer, not only the `+N`: at 1024 the shown
  // names are cut short too, and this is the one place they can be read.
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* A button so keyboard users can open the full list too. */}
        <button
          type="button"
          aria-label={`Maintainers: ${names.join(", ")}`}
          className="flex max-w-[130px] cursor-default text-left items-center text-sm xl:max-w-[280px]"
        >
          <span className="truncate">{shown.join(", ")}</span>
          {rest > 0 && (
            <span className="ml-1 shrink-0 text-muted-foreground">+{rest}</span>
          )}
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        {(agent.maintainers ?? []).map((m) => (
          <div key={m.user_id}>{m.display_name || "Unknown person"}</div>
        ))}
      </TooltipContent>
    </Tooltip>
  )
}

export function teamLibraryColumns({
  onEditAccess,
  onRemove,
  removingId,
}: TeamLibraryColumnHandlers): ColumnDef<LibraryAgentAdmin>[] {
  return [
    {
      accessorKey: "name",
      header: "Agent",
      cell: ({ row }) => {
        const agent = row.original
        const owner = agent.owner?.display_name
        return (
          <div className="min-w-0 max-w-[160px] xl:max-w-[320px]">
            <button
              type="button"
              className="block max-w-full truncate text-left font-medium text-primary hover:underline"
              onClick={() => onEditAccess(agent)}
            >
              {agent.name || agent.slug}
            </button>
            <p className="truncate text-xs text-muted-foreground">
              <span className="font-mono">{agent.slug}</span>
              {/* Only from xl: at 1024 the column is too narrow to show it
                  whole, and a name cut to two letters says nothing. */}
              {owner && (
                <span className="hidden xl:inline"> · added by {owner}</span>
              )}
            </p>
          </div>
        )
      },
    },
    {
      id: "maintainers",
      header: "Maintainers",
      cell: ({ row }) => <MaintainersCell agent={row.original} />,
    },
    {
      id: "audience",
      header: "Who can get it",
      cell: ({ row }) => (
        // Breaks only between facts, so a count stays with its unit.
        <span className="block min-w-[7rem] whitespace-normal text-sm">
          {audienceFacts(row.original.grants ?? []).map((fact, i) => (
            // The separator sits outside the nowrap span: that space is the
            // only place the line may break.
            <Fragment key={fact}>
              {i > 0 && " · "}
              <span className="whitespace-nowrap">{fact}</span>
            </Fragment>
          ))}
        </span>
      ),
    },
    {
      accessorKey: "updated_at",
      header: "Last updated",
      cell: ({ row }) =>
        row.original.head_sha ? (
          <RelativeTime
            timestamp={row.original.updated_at}
            showTooltip
            className="whitespace-nowrap text-sm"
          />
        ) : (
          <span className="whitespace-nowrap text-sm text-muted-foreground">
            Not shared yet
          </span>
        ),
    },
    {
      id: "actions",
      header: () => <span className="sr-only">Actions</span>,
      cell: ({ row }) => (
        <div className="flex justify-end">
          <TeamLibraryRowActions
            agent={row.original}
            removing={removingId === row.original.repo_id}
            onEditAccess={onEditAccess}
            onRemove={onRemove}
          />
        </div>
      ),
    },
  ]
}
