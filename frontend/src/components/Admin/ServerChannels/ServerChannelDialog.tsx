import { useQuery } from "@tanstack/react-query"
import { ArrowLeft } from "lucide-react"
import { useEffect, useState } from "react"

import {
  type ChannelTypePublic,
  type ServerChannelPublic,
  ServerChannelsService,
} from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { ChannelTypePicker } from "./ChannelTypePicker"
import { getChannelTypeMeta, getTransportShape } from "./channelTypes"
import { ServerChannelForm } from "./ServerChannelForm"

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** null = create. */
  channel: ServerChannelPublic | null
  /**
   * The channel types that already have a row. Only singleton types are read
   * from it — the picker greys those out instead of offering an "Add" that can
   * only ever come back 409.
   */
  existingChannelTypes?: string[]
  onCreated?: (channel: ServerChannelPublic) => void
}

/**
 * Add/edit a channel, in two steps: pick the chat app, then configure it.
 *
 * Same shape as "Add Credential": the type decides which fields exist at all,
 * so it is picked first from a card list rather than buried in the form as a
 * dropdown. Editing skips step 1 — the type is immutable after creation.
 */
export function ServerChannelDialog({
  open,
  onOpenChange,
  channel,
  existingChannelTypes,
  onCreated,
}: Props) {
  const isEdit = channel !== null
  const [selectedType, setSelectedType] = useState<string | null>(null)

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["serverChannelTypes"],
    queryFn: () => ServerChannelsService.listChannelTypes(),
    enabled: open,
  })
  const types = data ?? []

  // Reset on every open so a cancelled create never reopens on step 2 with the
  // previous pick, and so an edit always lands on its own channel's type.
  useEffect(() => {
    if (!open) return
    setSelectedType(channel?.channel_type ?? null)
  }, [open, channel])

  // May be undefined: an edit renders before (or without) the types fetch, and
  // a channel whose adapter left the registry has no entry at all. Kept
  // separate from the display name and the transport shape below so neither
  // has to be faked into a `ChannelTypePublic` that would claim a transport
  // shape nobody declared.
  const selectedEntry: ChannelTypePublic | undefined = selectedType
    ? types.find((t) => t.channel_type === selectedType)
    : undefined
  // Falls back to the slug rather than leaving the header blank.
  const selectedName = selectedEntry?.display_name ?? selectedType ?? ""
  const transport = getTransportShape(selectedEntry)
  const meta = selectedType ? getChannelTypeMeta(selectedType) : null
  const Icon = meta?.icon

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[560px] max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit channel" : "Add channel"}</DialogTitle>
          <DialogDescription>
            {selectedType
              ? "Let people outside the platform reach your agents from a chat app."
              : "Pick the chat app you want to connect."}
          </DialogDescription>
        </DialogHeader>

        {selectedType === null || meta === null ? (
          <ChannelTypePicker
            types={types}
            existingChannelTypes={existingChannelTypes}
            isLoading={isLoading}
            isError={isError}
            error={error}
            onSelect={(type) => setSelectedType(type.channel_type)}
          />
        ) : (
          <>
            {/* The chosen type stays on screen through step 2 — it drives the
                fields below, and on create it is still changeable. */}
            <div className="flex items-center gap-2 rounded-lg border bg-muted/40 px-3 py-2">
              {Icon && (
                <Icon className={`h-4 w-4 shrink-0 ${meta.iconClass}`} />
              )}
              <span className="text-sm font-medium">{selectedName}</span>
              {isEdit ? (
                <span className="ml-auto text-xs text-muted-foreground">
                  Type can't be changed after creation
                </span>
              ) : (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="ml-auto h-7 px-2 text-xs"
                  onClick={() => setSelectedType(null)}
                >
                  <ArrowLeft className="mr-1 h-3 w-3" />
                  Change
                </Button>
              )}
            </div>

            <ServerChannelForm
              // Rebuilds the form when the type changes — its fields, schema
              // and defaults are all type-derived.
              key={`${channel?.id ?? "new"}:${selectedType}`}
              channelType={selectedType}
              displayName={selectedName}
              transport={transport}
              channel={channel}
              onCancel={() => onOpenChange(false)}
              onSaved={(created) => {
                onOpenChange(false)
                if (created) onCreated?.(created)
              }}
            />
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
