import {
  type QueryClient,
  useMutation,
  useMutationState,
  useQueryClient,
} from "@tanstack/react-query"

import {
  type LLMPluginMarketplacePluginPublic,
  LlmPluginsService,
} from "@/client"
import { apiErrorCode } from "@/components/Admin/McpDirectory/mcpDirectoryApi"
import useCustomToast from "@/hooks/useCustomToast"
import { marketplaceEntriesKey } from "@/hooks/useMarketplaceSync"
import { getErrorMessage } from "@/utils"
import { plural } from "@/utils/marketplace"

type Entry = LLMPluginMarketplacePluginPublic
export type EntryAccess = "default" | "allowed" | "banned"

/** The server's cap on one bulk write. */
export const BULK_ACCESS_LIMIT = 500

const ACCESS_KEY = ["marketplace-entry", "access"] as const

export interface EntryAccessVariables {
  entry: Entry
  access: EntryAccess
  reason?: string | null
}

/**
 * What a write does, in the words of the row menu: it reads from where the
 * entry is coming *from* as well as where it is going, because "back to
 * default" is an Unban for a banned entry and a Remove for an allowed one.
 */
function verb({ entry, access }: EntryAccessVariables) {
  if (access === "allowed") {
    return { pending: "Allowing…", done: "allowed", failed: "allow" }
  }
  if (access === "banned") {
    return { pending: "Banning…", done: "banned", failed: "ban" }
  }
  return entry.access === "banned"
    ? { pending: "Unbanning…", done: "unbanned", failed: "unban" }
    : {
        pending: "Removing…",
        done: "removed from the allowlist",
        failed: "remove from the allowlist",
      }
}

function isFor(entryId: string) {
  return (mutation: { state: { variables: unknown } }) =>
    (mutation.state.variables as EntryAccessVariables | undefined)?.entry
      ?.id === entryId
}

/**
 * The write in flight on a row, named for the `⋯` slot's spinner, or `null`.
 * Read from the mutation cache by entry: one observer's `variables` describe
 * only its latest call.
 */
export function useEntryPending(entry: Entry): string | null {
  const pending = useMutationState({
    filters: {
      mutationKey: ACCESS_KEY,
      status: "pending",
      predicate: isFor(entry.id),
    },
    select: (mutation) =>
      mutation.state.variables as EntryAccessVariables | undefined,
  })
  const latest = pending[pending.length - 1]
  return latest ? verb(latest).pending : null
}

/** The same question outside render, for the row-click guard. */
export function isEntryPending(queryClient: QueryClient, entryId: string) {
  return (
    queryClient.isMutating({
      mutationKey: ACCESS_KEY,
      predicate: isFor(entryId),
    }) > 0
  )
}

function invalidate(queryClient: QueryClient, marketplaceId: string) {
  // The entries family carries the access counts; the marketplace row and
  // the marketplaces list their `plugin_count`.
  void queryClient.invalidateQueries({
    queryKey: marketplaceEntriesKey(marketplaceId),
  })
  void queryClient.invalidateQueries({
    queryKey: ["marketplace", marketplaceId],
  })
  void queryClient.invalidateQueries({ queryKey: ["marketplaces"] })
}

/** Allow, ban (with an optional reason), unban, or remove from the allowlist. */
export function useSetEntryAccess(marketplaceId: string) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  return useMutation({
    mutationKey: ACCESS_KEY,
    mutationFn: ({ entry, access, reason }: EntryAccessVariables) =>
      LlmPluginsService.setPluginAccess({
        pluginId: entry.id,
        requestBody: { access, reason: reason ?? null },
      }),
    onSuccess: (_, variables) => {
      showSuccessToast(`${variables.entry.name} ${verb(variables).done}`)
    },
    onError: (error, variables) => {
      const { failed } = verb(variables)
      showErrorToast(
        getErrorMessage(error, `Couldn't ${failed} ${variables.entry.name}`),
      )
    },
    onSettled: () => invalidate(queryClient, marketplaceId),
  })
}

/** Allow many entries in one write (bulk is Allow only, plan §C8). */
export function useAllowEntries(
  marketplaceId: string,
  onDone: (outcome: "allowed" | "stale") => void,
) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  return useMutation({
    mutationFn: (ids: string[]) =>
      LlmPluginsService.setMarketplaceEntriesAccess({
        marketplaceId,
        requestBody: { ids, access: "allowed" },
      }),
    onSuccess: ({ updated }) => {
      showSuccessToast(`Allowed ${plural(updated, "entry", "entries")}`)
      onDone("allowed")
    },
    onError: (error) => {
      if (apiErrorCode(error) === "foreign_ids") {
        // A sync removed some of them while they sat selected.
        showErrorToast(
          "Some selected entries are no longer in this marketplace — the list has been refreshed, select again.",
        )
        onDone("stale")
        return
      }
      showErrorToast(getErrorMessage(error, "Couldn't allow the entries"))
    },
    onSettled: () => invalidate(queryClient, marketplaceId),
  })
}
