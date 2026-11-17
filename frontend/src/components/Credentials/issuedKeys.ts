import { keepPreviousData, useQuery } from "@tanstack/react-query"

import { CredentialsService, type IssuedAgentApiKeyPublic } from "@/client"
import { isTerminalAgentApiKeyStatus } from "@/components/Credentials/agentApiKeyCopy"

/**
 * Keys the caller issued to OTHER people, across all their producers — the
 * "Issued Credentials" section of `/credentials`. These are not credentials the
 * caller owns (each lives in its recipient's credentials), so they never go
 * through the owned-row model or its server `category`.
 */

/**
 * Under the `["credentials"]` prefix on purpose: every credential mutation
 * (a key issued from the dialog, a revoke) already invalidates that prefix.
 */
export const issuedAgentApiKeysQueryKey = (
  workspaceFilter: string | undefined,
  includeEnded: boolean,
) => ["credentials", "issued-agent-api-keys", workspaceFilter, includeEnded]

/** The revoke mutation's key; a row reads its own pending state from it. */
export const revokeIssuedKeyMutationKey = ["revoke-issued-agent-api-key"]

export function useIssuedAgentApiKeys(
  workspaceFilter: string | undefined,
  includeEnded: boolean,
) {
  return useQuery({
    queryKey: issuedAgentApiKeysQueryKey(workspaceFilter, includeEnded),
    queryFn: () =>
      CredentialsService.readIssuedAgentApiKeys({
        includeEnded,
        userWorkspaceId: workspaceFilter,
      }),
    // Flipping "With ended keys" keeps the table (and the tab count) on
    // screen until the other list arrives, instead of dropping to skeletons.
    placeholderData: keepPreviousData,
  })
}

/** Full name, else email; the account may be gone. Wire strings use `||`. */
export function issuedKeyRecipientLabel(key: IssuedAgentApiKeyPublic): string {
  return key.issued_to?.full_name || key.issued_to?.email || "Deleted user"
}

export function issuedKeyLabel(key: IssuedAgentApiKeyPublic): string {
  return key.label || "API key"
}

/** Revocable: still working or paused, and its producer still exists. */
export function canRevokeIssuedKey(key: IssuedAgentApiKeyPublic): boolean {
  return !!key.producer && !isTerminalAgentApiKeyStatus(key.status)
}

export function matchesIssuedKeyQuery(
  key: IssuedAgentApiKeyPublic,
  query: string,
): boolean {
  return [
    key.issued_to?.full_name,
    key.issued_to?.email,
    key.producer?.name,
    key.label,
  ]
    .filter(Boolean)
    .some((field) => (field as string).toLowerCase().includes(query))
}
