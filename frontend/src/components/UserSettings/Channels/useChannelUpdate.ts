/**
 * The one write path for a channel's own settings.
 *
 * Shared by the row's `Switch` and its "Follow the default again" item, and by
 * every control in the Configure Sheet, so the three properties this write
 * depends on are stated once instead of being re-derived per call site:
 *
 * 1. **The list's in-flight reads are cancelled first.** The response is
 *    written into the cache rather than invalidating it (see
 *    `writeChannelToCache`), and a refetch that started before the write —
 *    a window-focus one, say — would land after it and restore pre-write
 *    data. React Query cannot know the cache was written by a mutation; the
 *    cancel is what makes the write authoritative.
 *
 * 2. **Writes to one channel are serialized.** `scope` queues same-id
 *    mutations instead of running them concurrently, and keeps `isPending`
 *    true while one is queued. The agent checklist sends the WHOLE
 *    `agent_ids` list on every tick, built from the last row the server
 *    returned, so two concurrent ticks would have the second overwrite the
 *    first. Until now only a `disabled` attribute prevented that — a
 *    rendering-level guard for a request-level hazard.
 *
 * 3. **Feedback lives on the mutation, not on the call site.** A per-call
 *    `onSuccess` is dropped when the component that fired it unmounts (a row
 *    inside a "Show all" Sheet that closes mid-write), which is exactly when
 *    the user most needs to be told what happened.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query"

import { UserChannelsService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import {
  type ChannelPatch,
  USER_CHANNELS_KEY,
  writeChannelToCache,
} from "./channelScopes"

export function useChannelUpdate(channelId: string) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  return useMutation({
    scope: { id: `channel-${channelId}` },
    mutationFn: (body: ChannelPatch) =>
      UserChannelsService.updateMyChannel({
        channelId,
        requestBody: body,
      }),
    onMutate: () => queryClient.cancelQueries({ queryKey: USER_CHANNELS_KEY }),
    onSuccess: (updated, body) => {
      writeChannelToCache(queryClient, updated)
      // The only patch whose effect can be invisible: reverting to the
      // administrator's default may leave the switch exactly where it was.
      // Everything else on this path is visible in the control the user just
      // moved, and a toast for it would be noise.
      if (body.is_enabled === null) {
        showSuccessToast("Back to the administrator's default")
      }
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to save channel settings")),
  })
}
