import { Link2Off, Loader2, Pin } from "lucide-react"

import type { ArtifactLinkPublic } from "@/client"
import {
  ListRow,
  RowFlag,
  RowInfo,
  type RowStatus,
} from "@/components/Common/ListRow"
import { formatRelativeTimestamp } from "@/components/Common/RelativeTime"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

export function linkDisplayName(link: ArtifactLinkPublic): string {
  return link.label || `Link ${link.token_prefix}…`
}

function linkStatus(link: ArtifactLinkPublic): RowStatus {
  if (link.blocked) {
    return { tone: "error", label: "Blocked — too many wrong codes" }
  }
  if (link.expired) return { tone: "warning", label: "Expired" }
  const expires = link.expires_at && formatRelativeTimestamp(link.expires_at)
  return {
    tone: "on",
    label: expires ? `Works — expires ${expires}` : "Works — never expires",
  }
}

interface ArtifactLinkRowProps {
  link: ArtifactLinkPublic
  revoking: boolean
  onRevoke: (link: ArtifactLinkPublic) => void
}

/**
 * One public link (the `GuestShareCard` row, adapted). No "Copy link": the
 * token is stored hashed, so the create result was the only place it showed.
 * Revoke is the row's only action, hover-revealed.
 */
export function ArtifactLinkRow({
  link,
  revoking,
  onRevoke,
}: ArtifactLinkRowProps) {
  const name = linkDisplayName(link)
  const created = formatRelativeTimestamp(link.created_at)
  const expires = link.expires_at && formatRelativeTimestamp(link.expires_at)

  return (
    <ListRow
      status={linkStatus(link)}
      muted={link.expired || link.blocked}
      title={name}
      badges={
        link.code ? (
          <Badge variant="outline" className="h-5 shrink-0 font-mono">
            {link.code}
          </Badge>
        ) : undefined
      }
      flags={
        <>
          {link.pinned_revision_number != null && (
            <RowFlag
              icon={Pin}
              label={`Always shows revision ${link.pinned_revision_number}`}
            />
          )}
          <RowInfo
            facts={[
              `Starts with ${link.token_prefix}`,
              created && `Created ${created}`,
              link.expires_at
                ? expires &&
                  `${link.expired ? "Expired" : "Expires"} ${expires}`
                : "Never expires",
            ]}
          />
        </>
      }
    >
      {revoking ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <span
              className="flex h-7 w-7 items-center justify-center"
              aria-live="polite"
            >
              <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
              <span className="sr-only">Revoking {name}…</span>
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
              type="button"
              variant="ghost"
              size="icon"
              className="h-7 w-7 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
              aria-label={`Revoke ${name}`}
              onClick={() => onRevoke(link)}
            >
              <Link2Off className="h-3.5 w-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top" className="text-xs">
            Revoke link
          </TooltipContent>
        </Tooltip>
      )}
    </ListRow>
  )
}
