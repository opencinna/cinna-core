import { useQuery } from "@tanstack/react-query"

import type { AgentApiKeyHolderInfo, CredentialWithData } from "@/client"
import { AgentApiService, CredentialsService } from "@/client"

/** Query key for a producer's API access surface (people, scopes, keys). */
export const agentApiAccessQueryKey = (agentId: string) => [
  "agentApiAccess",
  agentId,
]

/** Query key for the holder's view of one issued key. */
export const agentApiKeyHolderQueryKey = (credentialId: string) => [
  "agentApiKeyHolder",
  credentialId,
]

/**
 * Everyone with access to a producer's API, grouped by person, plus the two
 * producer switches (``GET /agents/{id}/agent-api/access``). Owner-gated: a
 * non-owner gets a 404, never a 403, so retrying only multiplies a request that
 * will never succeed.
 */
export function useAgentApiAccess(agentId: string, enabled = true) {
  return useQuery({
    queryKey: agentApiAccessQueryKey(agentId),
    queryFn: () => AgentApiService.listAgentApiAccess({ agentId }),
    enabled: !!agentId && enabled,
    retry: false,
    staleTime: 30_000,
  })
}

export interface AgentApiCredentialKind {
  /** The holder's view of the key, when this credential is an issued key. */
  holder: AgentApiKeyHolderInfo | undefined
  /** True once we know which of the two modes this credential is in. */
  isResolved: boolean
  /**
   * The mode could not be determined — a transient failure, not a 400/404.
   * The caller must NOT fall back to "connection": that would render a key as
   * a different product with its value hidden.
   */
  isUnresolvable: boolean
  error: unknown
  refetch: () => void
}

/**
 * Resolve whether an ``agent_api`` credential is an issued **key** or a
 * machine **connection**, and hand back the holder's view when it is a key.
 *
 * ``GET /credentials/{id}/agent-api-key`` is the authority:
 *   - 200 → an issued key, and the response is exactly what the detail page
 *     renders (status, producer, issuer, scopes, expiry, rotation grace);
 *   - 400 → the credential exists but is not an issued key: a connection;
 *   - 404 → not the caller's credential. A key is never shared, so a viewer
 *     who is not its owner can only be looking at a (shared) connection.
 * Any other failure is ``isUnresolvable`` rather than quietly "connection".
 *
 * ``retry: false`` because the 400/404 are expected, load-bearing answers.
 */
export function useAgentApiKeyForCredential(
  credential: CredentialWithData,
): AgentApiCredentialKind {
  const isAgentApi = credential.type === "agent_api"

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: agentApiKeyHolderQueryKey(credential.id),
    queryFn: () => CredentialsService.readAgentApiKey({ id: credential.id }),
    enabled: isAgentApi,
    retry: false,
    staleTime: 30_000,
  })

  if (!isAgentApi) {
    return {
      holder: undefined,
      isResolved: true,
      isUnresolvable: false,
      error: null,
      refetch,
    }
  }

  const status = (error as { status?: number } | null)?.status
  const isNotAKey = status === 400 || status === 404

  // A failed background refetch must not replace a loaded key page with an
  // error: that would unmount the card holding a just-issued or just-rotated
  // value. Only an answer we never had is unresolvable.
  return {
    holder: data,
    isResolved: data !== undefined || !!error || !isLoading,
    isUnresolvable: !!error && !isNotAKey && data === undefined,
    error,
    refetch,
  }
}
