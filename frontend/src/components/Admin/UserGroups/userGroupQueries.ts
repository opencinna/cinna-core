import type { QueryClient } from "@tanstack/react-query"

import { SHARED_AI_CREDENTIALS_KEY } from "@/components/Environments/useSharedAICredentials"
import {
  USER_GROUP_SHARE_TYPES,
  type UserGroupShareType,
} from "@/utils/userGroupShareTypes"

/**
 * Query keys for the admin User Groups surfaces (plan §8). Everything about one
 * group sits under `["user-group", id]`, so a membership or share change
 * invalidates that prefix and the list's `["user-groups"]`, and nothing else.
 */
export const userGroupKeys = {
  list: ["user-groups"] as const,
  detail: (groupId: string) => ["user-group", groupId] as const,
  members: (groupId: string) => ["user-group", groupId, "members"] as const,
  shares: (groupId: string) => ["user-group", groupId, "shares"] as const,
  deleteImpact: (groupId: string) =>
    ["user-group", groupId, "delete-impact"] as const,
  removalImpact: (groupId: string, userId: string) =>
    ["user-group", groupId, "removal-impact", userId] as const,
  revokeImpact: (groupId: string, type: string, resourceId: string) =>
    ["user-group", groupId, "revoke-impact", type, resourceId] as const,
}

/** After any write to a group: its own keys plus the list's counts. */
export function invalidateUserGroup(
  queryClient: QueryClient,
  groupId: string,
): Promise<unknown> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: userGroupKeys.detail(groupId) }),
    queryClient.invalidateQueries({ queryKey: userGroupKeys.list }),
  ])
}

/**
 * After an owner-side write to a group's share (a bundle grant or API scope
 * grant changed from the resource's own page): the groups list's share
 * counts and every open group's "Shared with this group" tab. Prefix keys,
 * so only queries that are mounted refetch.
 */
export function invalidateUserGroupShareLists(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: userGroupKeys.list })
  void queryClient.invalidateQueries({ queryKey: ["user-group"] })
}

/** The resource's own keys, per share type, after a share to it changed. */
function resourceKeys(
  type: UserGroupShareType,
  resourceId?: string,
): readonly (readonly unknown[])[] {
  // Without an id, each detail key is matched by its prefix (every resource).
  const id = resourceId === undefined ? [] : [resourceId]
  const catalog = [
    ["catalog"],
    ["catalog-items"],
    ["catalog-item", ...id],
    ["catalog-item-grants", ...id],
  ]
  return {
    agent_bundle: catalog,
    desktop_agent: catalog,
    skill_package: catalog,
    credential: [
      ["credentials"],
      ["credentials-shared-with-me"],
      ["credential", ...id],
      ["credential-group-shares", ...id],
    ],
    ai_credential: [
      ["aiCredentials"],
      ["aiCredentialsList"],
      ["ai-credential-group-shares", ...id],
      SHARED_AI_CREDENTIALS_KEY,
    ],
    artifact: [["artifacts"], ["artifact", ...id]],
    knowledge_source: [["knowledge-sources"], ["knowledge-source", ...id]],
    // The producer's API access card, and the Bundle Permissions card that
    // shows the same group grant on a bundle consuming this producer.
    agent_api_scopes: [
      ["agentApiAccess", ...id],
      ["bundlePermissionsOverview"],
    ],
  }[type]
}

/**
 * The resource's own keys, after a share to it was revoked here: its share
 * lists and its owner's / recipients' lists read the grant this removed.
 */
export function invalidateRevokedResource(
  queryClient: QueryClient,
  type: UserGroupShareType,
  resourceId: string,
): void {
  for (const queryKey of resourceKeys(type, resourceId)) {
    void queryClient.invalidateQueries({ queryKey })
  }
}

/**
 * After a group is deleted: every share made to it went with it, so every
 * family's lists and share panels are invalidated by prefix.
 */
export function invalidateAllSharedResources(queryClient: QueryClient): void {
  for (const type of Object.keys(
    USER_GROUP_SHARE_TYPES,
  ) as UserGroupShareType[]) {
    for (const queryKey of resourceKeys(type)) {
      void queryClient.invalidateQueries({ queryKey })
    }
  }
}
