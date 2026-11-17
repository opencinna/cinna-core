import { useIsMutating } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { Ban, Loader2 } from "lucide-react"

import type { IssuedCredentialPublic } from "@/client"
import { AgentBadge } from "@/components/Common/AgentBadge"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { agentApiKeyStatusLabel } from "@/components/Credentials/agentApiKeyCopy"
import {
  canRevokeIssued,
  ISSUED_KIND_LABEL,
  issuedCredentialLabel,
  issuedHolderLabel,
  issuedKindHint,
  revokeIssuedCredentialMutationKey,
} from "@/components/Credentials/issuedCredentials"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

/** Pending revokes of this row, read from the list-level mutation. */
export function isRevokingRow(variables: unknown, rowId: string): boolean {
  return (variables as IssuedCredentialPublic | undefined)?.id === rowId
}

function useRevoking(rowId: string): boolean {
  return (
    useIsMutating({
      mutationKey: revokeIssuedCredentialMutationKey,
      predicate: (m) => isRevokingRow(m.state.variables, rowId),
    }) > 0
  )
}

/** A relative time with the full date on hover, or nothing (§2 Absent facts). */
function TimeCell({ ts }: { ts: string | null | undefined }) {
  return ts ? (
    <RelativeTime
      timestamp={ts}
      showTooltip
      className="truncate text-xs text-muted-foreground"
    />
  ) : null
}

/**
 * The label is the row's real link — to the agent's Integrations tab, where
 * every kind is managed — so keyboard and middle-click reach what a row click
 * does. Under it: the prefix and the kind's one fact.
 */
function LabelCell({ row }: { row: IssuedCredentialPublic }) {
  // No label: the prefix is the name, rather than repeating the Type word.
  const label = row.label || `${row.token_prefix}…`
  const hint = issuedKindHint(row)
  const nameClass = cn(
    "block truncate text-sm font-medium rounded-sm outline-none",
    !row.label && "font-mono",
  )
  return (
    <div className="min-w-0">
      {row.agent ? (
        <Link
          to="/agent/$agentId"
          params={{ agentId: row.agent.id }}
          hash="integrations"
          className={cn(
            nameClass,
            "hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/50",
          )}
        >
          {label}
        </Link>
      ) : (
        <span className={nameClass}>{label}</span>
      )}
      {(row.label || hint) && (
        <span className="block truncate text-xs text-muted-foreground">
          {row.label && <span className="font-mono">{row.token_prefix}…</span>}
          {row.label && hint && " · "}
          {hint}
        </span>
      )}
    </div>
  )
}

const STATUS_LABEL: Record<IssuedCredentialPublic["status"], string> = {
  active: "Active",
  paused: "Paused",
  expired: "Expired",
  revoked: "Revoked",
  agent_removed: "Agent removed",
}

/**
 * The normalized status as one badge; nothing for a working token (§2 Row
 * state — "Active" on every row says nothing). An API key's own, finer status
 * ("API turned off by producer") rides in the tooltip when it adds words.
 */
function StatusCell({ row }: { row: IssuedCredentialPublic }) {
  if (row.status === "active") return null
  const badge = (
    <Badge
      variant={row.status === "paused" ? undefined : "destructive"}
      className={cn(
        "h-5",
        row.status === "paused" &&
          "border-transparent bg-warning text-warning-foreground",
      )}
    >
      {STATUS_LABEL[row.status]}
    </Badge>
  )
  if (row.kind !== "agent_api_key" || row.raw_status === row.status) {
    return badge
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>{badge}</TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        {agentApiKeyStatusLabel(row.raw_status)}
      </TooltipContent>
    </Tooltip>
  )
}

function ActionsCell({
  row,
  onRevoke,
}: {
  row: IssuedCredentialPublic
  onRevoke: (row: IssuedCredentialPublic) => void
}) {
  const revoking = useRevoking(row.id)
  if (!canRevokeIssued(row)) return null

  if (revoking) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className="flex h-7 w-7 items-center justify-center"
            aria-live="polite"
          >
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
            <span className="sr-only">Revoking…</span>
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          Revoking…
        </TooltipContent>
      </Tooltip>
    )
  }

  const noun = ISSUED_KIND_LABEL[row.kind]
  // The row's only action, and destructive: hover-revealed (§2 Row actions).
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 text-muted-foreground opacity-0 transition-opacity hover:text-destructive focus-visible:opacity-100 group-hover/row:opacity-100"
          aria-label={`Revoke ${issuedCredentialLabel(row)}${
            row.holder_kind === "person" ? ` for ${issuedHolderLabel(row)}` : ""
          }`}
          onClick={() => onRevoke(row)}
        >
          <Ban className="h-3.5 w-3.5" />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        Revoke {noun}
      </TooltipContent>
    </Tooltip>
  )
}

/**
 * The "Issued Credentials" table. `table-fixed`: every text cell truncates.
 * The label column takes the remaining width; expiry and last use join from
 * `2xl`, where the row has room for them beside the agent and holder.
 */
export function getIssuedCredentialColumns({
  onRevoke,
}: {
  onRevoke: (row: IssuedCredentialPublic) => void
}): ColumnDef<IssuedCredentialPublic>[] {
  return [
    {
      id: "type",
      header: "Type",
      meta: { className: "w-28" },
      cell: ({ row }) => (
        <span className="truncate text-sm">
          {ISSUED_KIND_LABEL[row.original.kind]}
        </span>
      ),
    },
    {
      id: "agent",
      header: "Agent",
      meta: { className: "w-[18%]" },
      cell: ({ row }) =>
        row.original.agent ? (
          <AgentBadge agent={row.original.agent} className="max-w-full" />
        ) : (
          <span className="text-xs text-muted-foreground">Deleted agent</span>
        ),
    },
    {
      id: "holder",
      header: "Holder",
      meta: { className: "w-[18%]" },
      cell: ({ row }) => (
        <span
          className={cn(
            "block truncate text-sm",
            row.original.holder_kind === "external_client" &&
              "text-muted-foreground",
          )}
        >
          {issuedHolderLabel(row.original)}
        </span>
      ),
    },
    {
      id: "label",
      header: "Label",
      cell: ({ row }) => <LabelCell row={row.original} />,
    },
    {
      id: "status",
      header: "Status",
      meta: { className: "w-28" },
      cell: ({ row }) => <StatusCell row={row.original} />,
    },
    {
      id: "expires",
      header: "Expires",
      meta: { className: "hidden w-32 2xl:table-cell" },
      cell: ({ row }) => <TimeCell ts={row.original.expires_at} />,
    },
    {
      id: "last_used",
      header: "Last used",
      meta: { className: "hidden w-32 2xl:table-cell" },
      cell: ({ row }) => <TimeCell ts={row.original.last_used_at} />,
    },
    {
      id: "actions",
      header: () => <span className="sr-only">Actions</span>,
      meta: { className: "w-12 px-2 text-right" },
      cell: ({ row }) => (
        <div className="flex justify-end">
          <ActionsCell row={row.original} onRevoke={onRevoke} />
        </div>
      ),
    },
  ]
}
