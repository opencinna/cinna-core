import {
  type QueryClient,
  useIsMutating,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"

import { type McpDirectoryEntryPublic, McpDirectoryService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { MCP_DIRECTORY_KEY } from "./mcpDirectoryCopy"

/**
 * The stable `detail.code` the MCP directory routes answer with
 * (`{code, message}`), or `undefined` for anything else.
 */
export function apiErrorCode(error: unknown): string | undefined {
  const detail = (error as { body?: { detail?: unknown } } | null)?.body?.detail
  if (detail && typeof detail === "object") {
    const code = (detail as { code?: unknown }).code
    if (typeof code === "string") return code
  }
  return undefined
}

export function useMcpDirectoryEntries() {
  return useQuery({
    queryKey: MCP_DIRECTORY_KEY,
    queryFn: () => McpDirectoryService.listMcpDirectoryEntries(),
  })
}

const ACCESS_KEY = ["mcp-directory", "access"] as const
const DELETE_KEY = ["mcp-directory", "delete"] as const

type Entry = McpDirectoryEntryPublic

export interface AccessVariables {
  entry: Entry
  access: "default" | "banned"
  reason?: string | null
}

/** Is a write with these variables about this entry? */
function isFor(entryId: string) {
  return (mutation: { state: { variables: unknown } }) =>
    (mutation.state.variables as { entry?: Entry } | undefined)?.entry?.id ===
    entryId
}

/**
 * Which write is in flight on a row, for the `⋯` slot's named spinner. Read
 * from the mutation cache by entry, not from one observer's `variables`
 * (which describe only its latest call).
 */
export function useRowPending(entry: Entry): string | null {
  const access = useIsMutating({
    mutationKey: ACCESS_KEY,
    predicate: isFor(entry.id),
  })
  const deleting = useIsMutating({
    mutationKey: DELETE_KEY,
    predicate: isFor(entry.id),
  })
  if (deleting > 0) return "Deleting…"
  if (access > 0) {
    return entry.access === "banned" ? "Unbanning…" : "Banning…"
  }
  return null
}

/**
 * The same question outside render: is any write on this entry in flight?
 * A row click must not open the Edit Sheet over a pending write — the
 * `DataTable` guard cannot see the spinner, which is a `span`.
 */
export function isRowPending(queryClient: QueryClient, entryId: string) {
  return (
    queryClient.isMutating({
      mutationKey: ACCESS_KEY,
      predicate: isFor(entryId),
    }) +
      queryClient.isMutating({
        mutationKey: DELETE_KEY,
        predicate: isFor(entryId),
      }) >
    0
  )
}

/** Ban (with an optional reason) or unban an entry. */
export function useSetEntryAccess() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  return useMutation({
    mutationKey: ACCESS_KEY,
    mutationFn: ({ entry, access, reason }: AccessVariables) =>
      McpDirectoryService.setMcpDirectoryEntryAccess({
        entryId: entry.id,
        requestBody: { access, reason: reason ?? null },
      }),
    onSuccess: (_, { entry, access }) => {
      showSuccessToast(
        access === "banned"
          ? `${entry.title} banned`
          : `${entry.title} is visible again`,
      )
    },
    onError: (error, { entry, access }) => {
      showErrorToast(
        getErrorMessage(
          error,
          access === "banned"
            ? `Couldn't ban ${entry.title}`
            : `Couldn't unban ${entry.title}`,
        ),
      )
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: MCP_DIRECTORY_KEY }),
  })
}

export function useDeleteEntry() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  return useMutation({
    mutationKey: DELETE_KEY,
    mutationFn: ({ entry }: { entry: Entry }) =>
      McpDirectoryService.deleteMcpDirectoryEntry({ entryId: entry.id }),
    onSuccess: (_, { entry }) => showSuccessToast(`${entry.title} deleted`),
    onError: (error, { entry }) =>
      showErrorToast(getErrorMessage(error, `Couldn't delete ${entry.title}`)),
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: MCP_DIRECTORY_KEY }),
  })
}
