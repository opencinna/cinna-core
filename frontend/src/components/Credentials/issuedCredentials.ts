import { keepPreviousData, useQuery } from "@tanstack/react-query"

import {
  AccessTokensService,
  AcpConnectorsService,
  AgentApiService,
  CredentialsService,
  type IssuedCredentialKind,
  type IssuedCredentialPublic,
  type IssuedCredentialStatus,
} from "@/client"
import { agentApiAccessQueryKey } from "@/hooks/useAgentApiKeys"

/**
 * Access tokens the caller issued for their agents — API keys given to other
 * people, A2A tokens and ACP tokens — the "Issued Credentials" section of
 * `/credentials`. These are not credentials the caller holds, so they never go
 * through the owned-row model or its server `category`.
 */

/** "All types" in the Filters menu: no `kind` sent to the server. */
export type IssuedKindFilter = IssuedCredentialKind | "all"

/**
 * Under the `["credentials"]` prefix on purpose: every credential mutation
 * (a key issued from the dialog, a revoke) already invalidates that prefix.
 */
export const issuedCredentialsQueryKey = (
  workspaceFilter: string | undefined,
  includeEnded: boolean,
  kind: IssuedKindFilter,
) => ["credentials", "issued", workspaceFilter, includeEnded, kind]

/** The revoke mutation's key; a row reads its own pending state from it. */
export const revokeIssuedCredentialMutationKey = ["revoke-issued-credential"]

export function useIssuedCredentials(
  workspaceFilter: string | undefined,
  includeEnded: boolean,
  kind: IssuedKindFilter,
) {
  return useQuery({
    queryKey: issuedCredentialsQueryKey(workspaceFilter, includeEnded, kind),
    queryFn: () =>
      CredentialsService.readIssuedCredentials({
        includeEnded,
        userWorkspaceId: workspaceFilter,
        kind: kind === "all" ? undefined : [kind],
      }),
    // Changing a filter keeps the table (and the tab count) on screen until
    // the other list arrives, instead of dropping to skeletons.
    placeholderData: keepPreviousData,
  })
}

/** The Type column's word, also the label fallback and a search term. */
export const ISSUED_KIND_LABEL: Record<IssuedCredentialKind, string> = {
  agent_api_key: "API key",
  a2a_token: "A2A token",
  acp_token: "ACP token",
}

/** Filters-menu plurals. */
export const ISSUED_KIND_LABEL_PLURAL: Record<IssuedCredentialKind, string> = {
  agent_api_key: "API keys",
  a2a_token: "A2A tokens",
  acp_token: "ACP tokens",
}

/**
 * The person's full name, else email; "Deleted user" when that account is
 * gone; "External client" for A2A/ACP tokens, which no platform user holds.
 * Wire strings use `||` (an empty string arrives).
 */
export function issuedHolderLabel(row: IssuedCredentialPublic): string {
  if (row.holder_kind === "external_client") return "External client"
  return row.holder?.full_name || row.holder?.email || "Deleted user"
}

export function issuedCredentialLabel(row: IssuedCredentialPublic): string {
  return row.label || ISSUED_KIND_LABEL[row.kind]
}

/** A2A / ACP agent mode, in the words the agent page's rows use. */
function modeLabel(mode: string): string {
  return mode === "building" ? "Building" : "Conversation"
}

/**
 * The small kind-specific fact beside the prefix: a key's scopes, an A2A
 * token's mode, an ACP token's connector. `null` when there is nothing to say
 * (§2 Absent facts — no "No scopes" chip).
 */
export function issuedKindHint(row: IssuedCredentialPublic): string | null {
  const d = row.details
  if (d.kind === "agent_api_key") {
    return d.scopes?.length ? d.scopes.join(", ") : null
  }
  if (d.kind === "a2a_token") return modeLabel(d.mode)
  if (d.kind === "acp_token") return d.connector_name || null
  return null
}

const TERMINAL: ReadonlySet<IssuedCredentialStatus> = new Set([
  "expired",
  "revoked",
  "agent_removed",
])

export function isTerminalIssuedStatus(status: IssuedCredentialStatus) {
  return TERMINAL.has(status)
}

/** Revocable: still working or paused, and its agent still exists. */
export function canRevokeIssued(row: IssuedCredentialPublic): boolean {
  return !!row.agent && !isTerminalIssuedStatus(row.status)
}

/** One revoke, dispatched to the route that owns the row's kind. */
export function revokeIssuedCredential(
  row: IssuedCredentialPublic,
): Promise<unknown> {
  if (!row.agent) return Promise.reject(new Error("The agent was deleted."))
  const agentId = row.agent.id
  const d = row.details
  if (row.kind === "a2a_token") {
    return AccessTokensService.updateAccessToken({
      agentId,
      tokenId: row.id,
      requestBody: { is_revoked: true },
    })
  }
  if (row.kind === "acp_token" && d.kind === "acp_token") {
    return AcpConnectorsService.revokeAcpToken({
      agentId,
      connectorId: d.connector_id,
      tokenId: row.id,
    })
  }
  if (row.kind === "agent_api_key") {
    return AgentApiService.revokeAgentApiKey({ agentId, keyId: row.id })
  }
  return Promise.reject(new Error("This token can't be revoked here."))
}

/**
 * The per-agent queries the agent page's cards read for this kind — what a
 * revoke here must refresh besides `["credentials"]`.
 */
export function agentQueryKeysForKind(
  kind: IssuedCredentialKind,
  agentId: string,
): unknown[][] {
  switch (kind) {
    case "a2a_token":
      return [["access-tokens", agentId]]
    case "acp_token":
      // The connectors card counts tokens; each connector's dialog lists them.
      return [
        ["acp-connectors", agentId],
        ["acp-tokens", agentId],
      ]
    default:
      return [agentApiAccessQueryKey(agentId)]
  }
}

export function matchesIssuedQuery(
  row: IssuedCredentialPublic,
  query: string,
): boolean {
  return [
    issuedHolderLabel(row),
    row.holder?.full_name,
    row.holder?.email,
    row.agent?.name,
    row.label,
    ISSUED_KIND_LABEL[row.kind],
  ]
    .filter(Boolean)
    .some((field) => (field as string).toLowerCase().includes(query))
}
