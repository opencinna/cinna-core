import { ChevronRight } from "lucide-react"

import type { ChannelTypePublic } from "@/client"
import { Skeleton } from "@/components/ui/skeleton"
import { getErrorMessage } from "@/utils"
import { getChannelTypeMeta } from "./channelTypes"

interface Props {
  types: ChannelTypePublic[]
  /**
   * Channel types that already have a row. Consulted only for types declaring
   * `is_singleton` — everything else may legitimately be added again (two
   * Google Chat apps, three polled mailboxes).
   */
  existingChannelTypes?: string[]
  isLoading: boolean
  isError: boolean
  error: unknown
  /** Called with the picked type; the dialog then swaps in its form. */
  onSelect: (type: ChannelTypePublic) => void
}

/**
 * Step 1 of "Add channel": pick the chat app.
 *
 * A card per registered adapter rather than a dropdown, mirroring the "Add
 * Credential" picker: the type decides which fields the next step even has, so
 * it is a choice of *what you are setting up*, not one field among many.
 */
export function ChannelTypePicker({
  types,
  existingChannelTypes,
  isLoading,
  isError,
  error,
  onSelect,
}: Props) {
  const existing = new Set(existingChannelTypes ?? [])
  if (isLoading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-full" />
      </div>
    )
  }

  // A failed fetch must not render as "no channel types" — that reads as a
  // server with nothing installed and sends the admin off to fix the wrong end.
  if (isError) {
    return (
      <p className="py-6 text-center text-sm text-destructive">
        {getErrorMessage(error, "Couldn't load channel types.")}
      </p>
    )
  }

  if (types.length === 0) {
    return (
      <p className="py-6 text-center text-sm text-muted-foreground">
        No channel types are registered on this server.
      </p>
    )
  }

  return (
    <div className="space-y-2">
      {types.map((type) => {
        const meta = getChannelTypeMeta(type.channel_type)
        const Icon = meta.icon
        // A singleton type that already has its row can never be added again —
        // the backend answers 409. Shown disabled rather than hidden: an admin
        // looking for App MCP needs to find out that it exists and is edited
        // from the list, not that it is missing from this server.
        const taken = type.is_singleton && existing.has(type.channel_type)
        return (
          <button
            key={type.channel_type}
            type="button"
            disabled={taken}
            onClick={() => onSelect(type)}
            className="group flex w-full items-center gap-3 rounded-lg border p-3 text-left transition-colors hover:border-primary/50 hover:bg-muted/50 disabled:pointer-events-none disabled:opacity-60"
          >
            <span className="shrink-0 rounded-md border bg-muted/50 p-2">
              <Icon className={`h-4 w-4 ${meta.iconClass}`} />
            </span>
            <span className="min-w-0">
              <span className="block text-sm font-medium">
                {type.display_name}
              </span>
              <span className="block text-xs text-muted-foreground">
                {taken
                  ? "Already configured — edit it from the channel list."
                  : meta.tagline}
              </span>
            </span>
            {!taken && (
              <ChevronRight className="ml-auto h-4 w-4 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
            )}
          </button>
        )
      })}
    </div>
  )
}
