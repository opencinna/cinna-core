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
import { getChannelTypeMeta } from "./channelTypes"
import { ServerChannelForm } from "./ServerChannelForm"

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** null = create. */
  channel: ServerChannelPublic | null
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

  // Resolved from the registry, falling back to the slug: an edit renders
  // before (or without) the types fetch, and a channel whose adapter was
  // unregistered has no entry at all — neither may leave the header blank.
  const selected: ChannelTypePublic | null = selectedType
    ? (types.find((t) => t.channel_type === selectedType) ?? {
        channel_type: selectedType,
        display_name: selectedType,
      })
    : null
  const meta = selected ? getChannelTypeMeta(selected.channel_type) : null
  const Icon = meta?.icon

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[560px] max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit channel" : "Add channel"}</DialogTitle>
          <DialogDescription>
            {selected
              ? "Let people outside the platform reach your agents from a chat app."
              : "Pick the chat app you want to connect."}
          </DialogDescription>
        </DialogHeader>

        {selected === null || meta === null ? (
          <ChannelTypePicker
            types={types}
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
              <span className="text-sm font-medium">
                {selected.display_name}
              </span>
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
              key={`${channel?.id ?? "new"}:${selected.channel_type}`}
              channelType={selected.channel_type}
              displayName={selected.display_name}
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
