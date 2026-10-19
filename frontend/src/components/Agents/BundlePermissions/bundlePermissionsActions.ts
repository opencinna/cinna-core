import type { BundlePermissionProducer } from "@/client"
import { AgentApiService, BundlesService, InstallsService } from "@/client"

export type ScopesByProducer = Record<string, string[]>

/**
 * Who a permissions row or the add/edit modal is about: one person, or one
 * user group. Both hold bundle access and per-producer scopes the same way;
 * only the endpoints differ.
 */
export type PermissionTarget =
  | {
      kind: "user"
      id: string
      email?: string | null
      fullName?: string | null
    }
  | { kind: "group"; id: string; name: string }

export interface PlannedAction {
  key: string
  /** Producer agent id when this is a producer section (for cache invalidation). */
  producerAgentId?: string
  run: () => Promise<unknown>
}

export const sameScopes = (a: string[], b: string[]): boolean => {
  if (a.length !== b.length) return false
  const sa = new Set(a)
  return b.every((s) => sa.has(s))
}

export const extractError = (e: unknown): string => {
  const err = e as
    | { body?: { detail?: string }; message?: string }
    | null
    | undefined
  return err?.body?.detail || err?.message || "Request failed"
}

export const existingGrantFor = (
  producer: BundlePermissionProducer,
  userId: string,
) => producer.grants?.find((g) => g.user_id === userId)

export const existingGroupGrantFor = (
  producer: BundlePermissionProducer,
  groupId: string,
) => producer.group_grants?.find((g) => g.group_id === groupId)

/** The target's scope grant on one producer, whichever kind it is. */
export const scopeGrantFor = (
  producer: BundlePermissionProducer,
  target: Pick<PermissionTarget, "kind" | "id">,
) =>
  target.kind === "group"
    ? existingGroupGrantFor(producer, target.id)
    : existingGrantFor(producer, target.id)

interface BuildActionsParams {
  target: PermissionTarget
  isEdit: boolean
  agentId: string
  bundleAccessApplicable: boolean
  bundleUuid: string | null
  manageableProducers: BundlePermissionProducer[]
  scopesByProducer: ScopesByProducer
}

function bundleAction(
  target: PermissionTarget,
  agentId: string,
  bundleUuid: string | null,
): PlannedAction | null {
  if (target.kind === "group") {
    // The card grants a group `use` only; there is no level to pick.
    return {
      key: "bundle",
      run: () =>
        InstallsService.addBundleGroupGrant({
          agentId,
          requestBody: { group_id: target.id },
        }),
    }
  }
  const email = target.email
  if (!bundleUuid || !email) return null
  return {
    key: "bundle",
    run: () => BundlesService.addGrant({ bundleUuid, requestBody: { email } }),
  }
}

function createScopes(
  target: PermissionTarget,
  producerAgentId: string,
  scopes: string[],
) {
  return target.kind === "group"
    ? AgentApiService.createAgentApiGroupGrant({
        agentId: producerAgentId,
        requestBody: { group_id: target.id, scopes },
      })
    : AgentApiService.createAgentApiGrant({
        agentId: producerAgentId,
        requestBody: { user_id: target.id, scopes },
      })
}

function updateScopes(
  target: PermissionTarget,
  producerAgentId: string,
  grantId: string,
  scopes: string[],
) {
  return target.kind === "group"
    ? AgentApiService.updateAgentApiGroupGrant({
        agentId: producerAgentId,
        grantId,
        requestBody: { scopes },
      })
    : AgentApiService.updateAgentApiGrant({
        agentId: producerAgentId,
        grantId,
        requestBody: { scopes },
      })
}

/** The net changes a submit would make, one action per section. */
export const buildActions = ({
  target,
  isEdit,
  agentId,
  bundleAccessApplicable,
  bundleUuid,
  manageableProducers,
  scopesByProducer,
}: BuildActionsParams): PlannedAction[] => {
  const actions: PlannedAction[] = []

  // Bundle access — in ADD mode, adding a person or a group grants bundle
  // access (no opt-out). In EDIT mode we never touch bundle access: it was
  // granted when the row was created, and revocation happens only via the
  // row-level remove on the card, not per-field toggling here.
  if (!isEdit && bundleAccessApplicable) {
    const action = bundleAction(target, agentId, bundleUuid)
    if (action) actions.push(action)
  }

  // Per-manageable-producer scope sections.
  for (const p of manageableProducers) {
    const desired = scopesByProducer[p.producer_agent_id] ?? []
    const existing = scopeGrantFor(p, target)
    const key = `producer:${p.producer_agent_id}`
    if (!existing) {
      if (desired.length > 0) {
        actions.push({
          key,
          producerAgentId: p.producer_agent_id,
          run: () => createScopes(target, p.producer_agent_id, desired),
        })
      }
    } else if (!sameScopes(existing.scopes ?? [], desired)) {
      actions.push({
        key,
        producerAgentId: p.producer_agent_id,
        run: () =>
          updateScopes(target, p.producer_agent_id, existing.grant_id, desired),
      })
    }
  }
  return actions
}
