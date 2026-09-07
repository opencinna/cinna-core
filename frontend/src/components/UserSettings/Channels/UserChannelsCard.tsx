/**
 * Channels — Settings → Channels.
 *
 * One row per channel an administrator has made available to this user, with
 * the one thing the user controls at a glance: whether the channel is on for
 * them, and whether that was their choice or an administrator's default.
 * Everything structured — agent scope, the agent picks, the identity-routing
 * consent — lives one level down in the Configure Sheet, so nothing on this
 * card changes its height.
 *
 * The neighbouring cards on this tab are NOT older versions of this list. The
 * App MCP card holds the endpoint plus its connect walkthrough; the Identity
 * Server card holds the authoring side of identity sharing (which of my
 * agents, exposed to whom). Both would be unreachable if this card were the
 * only thing on the tab. "People who shared with you" is the consuming side of
 * the same person-level toggle, and is its own card because those switches are
 * per person and govern every surface identity reaches — not just channels.
 *
 * Mail Servers moved out entirely — server-owned infrastructure now, under
 * Admin → Server Configuration.
 *
 * The inherit rules are resolved by the backend and only read here; see
 * `channelScopes.ts` for why that is the rule this whole folder is organised
 * around.
 */
import { useQuery } from "@tanstack/react-query"
import { useMemo, useState } from "react"

import type { UserChannelPublic } from "@/client"
import { UserChannelsService } from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { AllChannelsSheet } from "./AllChannelsSheet"
import { ChannelConfigSheet } from "./ChannelConfigSheet"
import { ChannelRow } from "./ChannelRow"
import { USER_CHANNELS_KEY } from "./channelScopes"

/** Rows shown in the card; the rest live behind "Show all (N)". */
const PREVIEW_COUNT = 5

/** Stable identity so the sort below memoises while the query loads. */
const NO_CHANNELS: UserChannelPublic[] = []

export function UserChannelsCard() {
  const [isAllOpen, setIsAllOpen] = useState(false)
  // The id, not the row: the Configure Sheet must render the re-resolved row
  // each write returns, and a copy taken when it opened would go stale on the
  // first change.
  const [configuringId, setConfiguringId] = useState<string | null>(null)

  const {
    data: channels,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: USER_CHANNELS_KEY,
    queryFn: () => UserChannelsService.listMyChannels(),
  })

  // By name, and deliberately NOT "enabled first": the row's own Switch writes
  // `is_enabled`, so sorting on it would move a row out from under the finger
  // that just toggled it — and past five channels, out of the preview
  // entirely. Enabled-ness is already legible per row (`opacity-60` and the
  // metadata line), which is what the ordering would have been for.
  const sorted = useMemo(
    () =>
      [...(channels ?? NO_CHANNELS)].sort((a, b) =>
        a.name.localeCompare(b.name),
      ),
    [channels],
  )
  const totalCount = sorted.length

  const configuring =
    sorted.find((channel) => channel.id === configuringId) ?? null

  return (
    <Card>
      <CardHeader>
        {/* No primary button: a user cannot create a channel — an
            administrator connects one in Server Configuration. */}
        <CardTitle>Channels</CardTitle>
        <CardDescription>
          Chat apps an administrator has connected, and whether each one may
          reach you.
        </CardDescription>
      </CardHeader>

      <CardContent>
        {/* A failed fetch must never read as "no channels are configured" — a
            user would conclude nothing can reach them, which is the opposite
            of what an unknown state warrants. */}
        {isError ? (
          <QueryErrorAlert
            error={error}
            fallback="Couldn't load your channels"
            onRetry={() => refetch()}
          >
            <p className="text-xs">
              This is a failed request, not an empty list.
            </p>
          </QueryErrorAlert>
        ) : isLoading ? (
          <div className="space-y-1.5">
            <Skeleton className="h-[52px] w-full rounded-lg" />
            <Skeleton className="h-[52px] w-full rounded-lg" />
            <Skeleton className="h-[52px] w-full rounded-lg" />
          </div>
        ) : totalCount === 0 ? (
          <p className="text-sm text-muted-foreground">
            No channels are available to you yet — an administrator connects
            them in Server Configuration. If one you were using has disappeared
            from this list, anything you had set for it is kept and applies
            again if it comes back.
          </p>
        ) : (
          <div className="space-y-1.5">
            {sorted.slice(0, PREVIEW_COUNT).map((channel) => (
              <ChannelRow
                key={channel.id}
                channel={channel}
                onConfigure={(picked) => setConfiguringId(picked.id)}
              />
            ))}
            {totalCount > PREVIEW_COUNT && (
              <Button
                variant="link"
                size="sm"
                className="px-0"
                onClick={() => setIsAllOpen(true)}
              >
                Show all ({totalCount})
              </Button>
            )}
          </div>
        )}
      </CardContent>

      <AllChannelsSheet
        channels={sorted}
        open={isAllOpen}
        onOpenChange={setIsAllOpen}
        onConfigure={(picked) => setConfiguringId(picked.id)}
      />

      {/* One instance, owned here because it is opened from two hosts, and
          mounted only while open so its queries do not run behind the card. */}
      {configuring && (
        <ChannelConfigSheet
          channel={configuring}
          onClose={() => setConfiguringId(null)}
        />
      )}
    </Card>
  )
}
