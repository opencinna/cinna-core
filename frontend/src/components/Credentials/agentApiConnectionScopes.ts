import type { QueryClient } from "@tanstack/react-query"

/**
 * The coded refusal of the connection-scope routes (connect with ``scopes``,
 * ``PUT …/connections/{credential}/scopes``): ``unknown_scope`` is answered
 * inline under the scope editor, every other code goes to the error toast.
 */
export function unknownScopeMessage(error: unknown): string | null {
  const detail = (error as { body?: { detail?: unknown } } | null)?.body?.detail
  if (!detail || typeof detail !== "object") return null
  const { code, message } = detail as { code?: unknown; message?: unknown }
  if (code !== "unknown_scope") return null
  return typeof message === "string" && message
    ? message
    : "Some of these scopes aren't declared by the API."
}

/** Everything that shows a connection's scopes, after they change. */
export function invalidateConnectionScopeQueries(
  queryClient: QueryClient,
  credentialId: string,
  producerAgentId: string | null,
) {
  queryClient.invalidateQueries({
    queryKey: ["agentApiConnection", credentialId],
  })
  queryClient.invalidateQueries({ queryKey: ["credential", credentialId] })
  queryClient.invalidateQueries({ queryKey: ["credentials"] })
  queryClient.invalidateQueries({ queryKey: ["credentials-shared-with-me"] })
  if (producerAgentId) {
    queryClient.invalidateQueries({
      queryKey: ["agentApiConnections", producerAgentId],
    })
  }
}
