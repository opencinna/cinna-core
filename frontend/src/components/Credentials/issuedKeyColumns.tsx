import { useIsMutating } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { Ban, KeySquare, Loader2 } from "lucide-react"

import type { IssuedAgentApiKeyPublic } from "@/client"
import { AgentBadge } from "@/components/Common/AgentBadge"
import { RowInfo } from "@/components/Common/ListRow"
import { formatRelativeTimestamp } from "@/components/Common/RelativeTime"
import { AgentApiKeyStatusBadge } from "@/components/Credentials/AgentApiKeyStatusBadge"
import { CREDENTIAL_COLUMN_CLASSES } from "@/components/Credentials/credentialColumns"
import {
  canRevokeIssuedKey,
  issuedKeyLabel,
  issuedKeyRecipientLabel,
  revokeIssuedKeyMutationKey,
} from "@/components/Credentials/issuedKeys"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

/** "Used 3 days ago"; `null` when the timestamp is absent or unparseable. */
function rel(prefix: string, ts: string | null | undefined): string | null {
  const r = ts ? formatRelativeTimestamp(ts) : null
  return r ? `${prefix} ${r}` : null
}

function expiryFact(key: IssuedAgentApiKeyPublic): string | null {
  return key.status === "expired"
    ? rel("Expired", key.expires_at)
    : rel("Expires", key.expires_at)
}

/** Pending revokes of this key, read from the list-level mutation. */
function useRevoking(keyId: string): boolean {
  return (
    useIsMutating({
      mutationKey: revokeIssuedKeyMutationKey,
      predicate: (m) =>
        (m.state.variables as { keyId?: string } | undefined)?.keyId === keyId,
    }) > 0
  )
}

/**
 * Recipient (the row's link, to where the key is managed: the producer's API
 * access card), then the key's name and prefix, then its status badge — which,
 * as on every credential row, renders nothing for a working key.
 */
function RecipientCell({ apiKey }: { apiKey: IssuedAgentApiKeyPublic }) {
  const recipient = issuedKeyRecipientLabel(apiKey)
  const nameClass =
    "min-w-0 shrink truncate text-sm font-medium rounded-sm outline-none"

  return (
    <div className="flex min-w-0 items-center gap-2">
      <KeySquare
        className="h-4 w-4 shrink-0 text-muted-foreground"
        aria-hidden="true"
      />
      {apiKey.producer ? (
        <Link
          to="/agent/$agentId"
          params={{ agentId: apiKey.producer.id }}
          hash="integrations"
          className={`${nameClass} hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/50`}
        >
          {recipient}
        </Link>
      ) : (
        <span className={nameClass}>{recipient}</span>
      )}
      <span className="min-w-0 shrink-[2] truncate text-xs text-muted-foreground">
        {issuedKeyLabel(apiKey)} ·{" "}
        <span className="font-mono">{apiKey.token_prefix}…</span>
      </span>
      <AgentApiKeyStatusBadge status={apiKey.status} className="shrink-0" />
    </div>
  )
}

/** The producer, then (from `lg`) expiry and last use, then the detail flag. */
function FactsCell({ apiKey }: { apiKey: IssuedAgentApiKeyPublic }) {
  const inline = [rel("Used", apiKey.last_used_at), expiryFact(apiKey)]
    .filter(Boolean)
    .join(" · ")

  return (
    <div className="flex min-w-0 items-center justify-end gap-2">
      {inline && (
        <span className="hidden min-w-0 truncate text-xs text-muted-foreground lg:inline">
          {inline}
        </span>
      )}
      {apiKey.producer ? (
        <AgentBadge agent={apiKey.producer} className="min-w-0 shrink" />
      ) : (
        <span className="shrink-0 text-xs text-muted-foreground">
          Deleted agent
        </span>
      )}
      <span className="shrink-0">
        <RowInfo
          facts={[
            `Prefix ${apiKey.token_prefix}…`,
            rel("Last used", apiKey.last_used_at) || "Never used",
            expiryFact(apiKey) || "Never expires",
            rel("Issued", apiKey.created_at),
            rel("Revoked", apiKey.revoked_at),
          ]}
        />
      </span>
    </div>
  )
}

function ActionsCell({
  apiKey,
  onRevoke,
}: {
  apiKey: IssuedAgentApiKeyPublic
  onRevoke: (key: IssuedAgentApiKeyPublic) => void
}) {
  const revoking = useRevoking(apiKey.id)
  if (!canRevokeIssuedKey(apiKey)) return null

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

  // The row's only action, and destructive: hover-revealed (§2 Row actions).
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 text-muted-foreground opacity-0 transition-opacity hover:text-destructive focus-visible:opacity-100 group-hover/row:opacity-100"
          aria-label={`Revoke ${issuedKeyLabel(apiKey)} for ${issuedKeyRecipientLabel(apiKey)}`}
          onClick={() => onRevoke(apiKey)}
        >
          <Ban className="h-3.5 w-3.5" />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        Revoke key
      </TooltipContent>
    </Tooltip>
  )
}

/**
 * The "Issued Credentials" table: same widths as the credential columns so the
 * section reads as part of the same page, its own cells because these rows
 * are keys the caller issued, not credentials they hold.
 */
export function getIssuedKeyColumns({
  onRevoke,
}: {
  onRevoke: (key: IssuedAgentApiKeyPublic) => void
}): ColumnDef<IssuedAgentApiKeyPublic>[] {
  return [
    {
      id: "recipient",
      header: "Issued to",
      cell: ({ row }) => <RecipientCell apiKey={row.original} />,
    },
    {
      id: "facts",
      header: () => <span className="sr-only">Agent, use and expiry</span>,
      meta: { className: CREDENTIAL_COLUMN_CLASSES.facts },
      cell: ({ row }) => <FactsCell apiKey={row.original} />,
    },
    {
      id: "actions",
      header: () => <span className="sr-only">Actions</span>,
      meta: { className: CREDENTIAL_COLUMN_CLASSES.actions },
      cell: ({ row }) => (
        <div className="flex justify-end">
          <ActionsCell apiKey={row.original} onRevoke={onRevoke} />
        </div>
      ),
    },
  ]
}
