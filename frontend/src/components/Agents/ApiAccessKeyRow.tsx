import { Ban, Loader2, Lock } from "lucide-react"

import type { AgentApiKeyPublic } from "@/client"
import { ListRow, RowFlag, RowInfo } from "@/components/Common/ListRow"
import { formatRelativeTimestamp } from "@/components/Common/RelativeTime"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { apiAccessKeyStatus, isEndedKey, pluralize } from "./apiAccess"

interface ApiAccessKeyRowProps {
  apiKey: AgentApiKeyPublic
  /** The revoke for this key is in flight. */
  revoking?: boolean
  /** Omitted for ended keys, which have no action (plan D27). */
  onRevoke?: () => void
}

/** "Last used 3 days ago" — `null` parses to the fallback word. */
function rel(prefix: string, ts: string | null | undefined, never: string) {
  const r = ts ? formatRelativeTimestamp(ts) : null
  return r ? `${prefix} ${r}` : never
}

/**
 * One issued key inside a person's access dialog (S1a) — the connected-
 * machines shape (P5): a View row whose only mutation is "cut this one off",
 * so no `⋯`, and the single destructive control is hover-revealed. Ended keys
 * have no action at all: they stay for the record (plan D27, no purge).
 */
export function ApiAccessKeyRow({
  apiKey,
  revoking,
  onRevoke,
}: ApiAccessKeyRowProps) {
  const ended = isEndedKey(apiKey)
  const label = apiKey.label || "API key"
  const linked = apiKey.linked_agent_count ?? 0

  return (
    <ListRow
      status={apiAccessKeyStatus(apiKey)}
      muted={ended}
      title={label}
      flags={
        <>
          {apiKey.read_only && <RowFlag icon={Lock} label="Read-only key" />}
          <RowInfo
            facts={[
              `Prefix ${apiKey.token_prefix}…`,
              rel("Last used", apiKey.last_used_at, "Never used"),
              apiKey.status === "expired"
                ? rel("Expired", apiKey.expires_at, "Expired")
                : rel("Expires", apiKey.expires_at, "Never expires"),
              linked > 0 &&
                `Linked to ${pluralize(linked, "agent", "agents")}`,
              rel("Issued", apiKey.created_at, "Issued at an unknown time"),
              ended &&
                !!apiKey.revoked_at &&
                rel("Revoked", apiKey.revoked_at, "Revoked"),
            ]}
          />
        </>
      }
    >
      {!ended &&
        onRevoke &&
        (revoking ? (
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
        ) : (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted-foreground opacity-0 transition-opacity hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100"
                aria-label={`Revoke ${label}`}
                onClick={onRevoke}
              >
                <Ban className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top" className="text-xs">
              Revoke key
            </TooltipContent>
          </Tooltip>
        ))}
    </ListRow>
  )
}
