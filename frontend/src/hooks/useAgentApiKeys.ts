import { useQuery } from "@tanstack/react-query"

import type { AgentApiKeyPublic, CredentialWithData } from "@/client"
import { AgentApiService } from "@/client"

/** Query key for a producer agent's external-key list. */
export const agentApiKeysQueryKey = (agentId: string) => [
  "agentApiKeys",
  agentId,
]

/**
 * External keys issued on a producer agent (``GET /agents/{id}/agent-api/keys``).
 * Owner-gated server-side: a non-owner gets a 404, never a 403.
 */
export function useAgentApiKeys(agentId: string, enabled = true) {
  return useQuery({
    queryKey: agentApiKeysQueryKey(agentId),
    queryFn: () => AgentApiService.listAgentApiKeys({ agentId }),
    enabled: !!agentId && enabled,
    // A non-owner gets a deterministic 404 here, so retrying only multiplies a
    // request that will never succeed.
    retry: false,
    staleTime: 30_000,
  })
}

export interface AgentApiCredentialKind {
  /** Producer agent the credential points at, when it records one. */
  producerAgentId: string | undefined
  /** The key row backing this credential, when it is an external key. */
  key: AgentApiKeyPublic | undefined
  /** True once we know which of the two modes this credential is in. */
  isResolved: boolean
  /**
   * The mode could not be determined — a transient failure, not a 404. The
   * caller must NOT fall back to "connection": that would silently hide a key's
   * identity, scopes and value behind the wrong view.
   */
  isUnresolvable: boolean
}

/**
 * Resolve whether an ``agent_api`` credential is an external **key** or a
 * machine **connection** (plan §2), and hand back the key row when it is one.
 *
 * There is no per-credential "kind" endpoint: ``GET /credentials/{id}`` reports
 * a placeholder ``category`` (only the paginated list computes the real one),
 * so we ask the producer instead. Its key list is the authoritative view and
 * carries everything the detail card needs anyway — subject, expiry, prefix,
 * usability.
 *
 * Two outcomes mean "this is a connection":
 *   - a 200 whose list does not contain this credential id (the viewer owns the
 *     producer, and this simply is not one of its keys);
 *   - a 404. Only a producer owner can hold a key credential, so being unable
 *     to see the producer's keys rules "key" out.
 *
 * Any OTHER failure — a 500, a dropped connection — is reported as
 * ``isUnresolvable`` rather than quietly collapsing into "connection", since
 * that would render a key as a different product with its value hidden.
 *
 * ``retry: false`` because the 404 is an expected, load-bearing answer rather
 * than something worth retrying.
 */
export function useAgentApiKeyForCredential(
  credential: CredentialWithData,
): AgentApiCredentialKind {
  const isAgentApi = credential.type === "agent_api"
  const producerAgentId =
    (credential.credential_data?.producer_agent_id as string | undefined) ||
    undefined

  const { data, isLoading, error } = useQuery({
    queryKey: agentApiKeysQueryKey(producerAgentId ?? ""),
    queryFn: () =>
      AgentApiService.listAgentApiKeys({ agentId: producerAgentId as string }),
    enabled: isAgentApi && !!producerAgentId,
    retry: false,
    staleTime: 30_000,
  })

  // No producer recorded (or not an agent_api credential at all) — nothing to
  // ask, and the answer is already known: it cannot be a key.
  if (!isAgentApi || !producerAgentId) {
    return {
      producerAgentId,
      key: undefined,
      isResolved: true,
      isUnresolvable: false,
    }
  }

  const isNotFound = (error as { status?: number } | null)?.status === 404
  const isUnresolvable = !!error && !isNotFound

  return {
    producerAgentId,
    key: data?.data.find((k) => k.credential_id === credential.id),
    isResolved: !!error || (!isLoading && data !== undefined),
    isUnresolvable,
  }
}
