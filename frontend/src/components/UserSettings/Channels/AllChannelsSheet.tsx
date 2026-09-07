import type { UserChannelPublic } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { ChannelRow } from "./ChannelRow"

interface AllChannelsSheetProps {
  /** Every channel, already sorted the way the card sorts them. */
  channels: UserChannelPublic[]
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Opens the Configure Sheet — this Sheet closes itself first. */
  onConfigure: (channel: UserChannelPublic) => void
}

/**
 * The "Show all (N)" destination for the Channels card.
 *
 * A Sheet rather than a route: the full list needs no search, sort or
 * pagination, and a channel has no lifecycle of its own here — a user cannot
 * create or delete one. The rows are the card's rows — same component, same
 * actions — so the two hosts cannot drift.
 *
 * Configure closes this Sheet before opening the editor: chained, never
 * nested, so disclosure stays two levels deep. The row's Discard confirm is an
 * `AlertDialog` and is the one thing allowed to open over a Sheet.
 */
export function AllChannelsSheet({
  channels,
  open,
  onOpenChange,
  onConfigure,
}: AllChannelsSheetProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Channels</SheetTitle>
          <SheetDescription>{channels.length} channels</SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4">
          {channels.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No channels are available to you yet.
            </p>
          ) : (
            <ListRowGroup>
              {channels.map((channel) => (
                <ChannelRow
                  key={channel.id}
                  channel={channel}
                  onConfigure={(picked) => {
                    onOpenChange(false)
                    onConfigure(picked)
                  }}
                />
              ))}
            </ListRowGroup>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
