import type { UserGroupSharePublic } from "@/client"
import { catalogAccessLabel } from "@/utils/catalogAccess"
import type { CatalogKind } from "@/utils/catalogItems"

/**
 * The words for what can be shared with a user group — one module, so the
 * group's "Shared with this group" table, the delete / remove-member impact
 * and the revoke confirm all name a type the same way.
 */
export type UserGroupShareType = UserGroupSharePublic["resource_type"]

interface ShareTypeMeta {
  /** The Type badge in the shares table, and the noun in a confirm title. */
  label: string
  /** Count nouns for an impact sentence: "1 credential" / "3 credentials". */
  one: string
  many: string
  family: UserGroupShareFamily
}

export type UserGroupShareFamily =
  | "all"
  | "catalog"
  | "credential"
  | "ai_credential"
  | "artifact"
  | "knowledge_source"
  | "agent_api"

export const USER_GROUP_SHARE_TYPES: Record<UserGroupShareType, ShareTypeMeta> =
  {
    agent_bundle: {
      label: "Agent bundle",
      one: "agent bundle",
      many: "agent bundles",
      family: "catalog",
    },
    desktop_agent: {
      label: "Desktop agent",
      one: "desktop agent",
      many: "desktop agents",
      family: "catalog",
    },
    skill_package: {
      label: "Skill",
      one: "skill",
      many: "skills",
      family: "catalog",
    },
    credential: {
      label: "Credential",
      one: "credential",
      many: "credentials",
      family: "credential",
    },
    ai_credential: {
      label: "AI credential",
      one: "AI credential",
      many: "AI credentials",
      family: "ai_credential",
    },
    artifact: {
      label: "Artifact",
      one: "artifact",
      many: "artifacts",
      family: "artifact",
    },
    knowledge_source: {
      label: "Knowledge source",
      one: "knowledge source",
      many: "knowledge sources",
      family: "knowledge_source",
    },
    agent_api_scopes: {
      label: "API scopes",
      one: "API scope grant",
      many: "API scope grants",
      family: "agent_api",
    },
  }

/** Display order of the types; matches the server's sort (§7). */
const TYPE_ORDER = Object.keys(USER_GROUP_SHARE_TYPES) as UserGroupShareType[]

/** The resource's name as shown; an empty string arrives from the wire. */
export function shareResourceName(share: UserGroupSharePublic): string {
  return share.resource_name || "Untitled"
}

export function shareTypeLabel(type: string): string {
  return USER_GROUP_SHARE_TYPES[type as UserGroupShareType]?.label || type
}

export function shareFamily(type: UserGroupShareType): UserGroupShareFamily {
  return USER_GROUP_SHARE_TYPES[type].family
}

const NON_CATALOG_LEVELS: Record<string, string> = {
  read: "Can read",
  use: "Can use",
  view: "Can view",
  query: "Can query",
}

const CATALOG_KIND: Partial<Record<UserGroupShareType, CatalogKind>> = {
  agent_bundle: "cloud",
  desktop_agent: "desktop",
  skill_package: "skill",
}

/**
 * The catalog's words for a catalog row (kind-aware for `edit`), the scope
 * names for an API scope grant, "Can read" … otherwise.
 */
export function shareLevelLabel(share: UserGroupSharePublic): string {
  const kind = CATALOG_KIND[share.resource_type]
  if (kind) return catalogAccessLabel(kind, share.level)
  if (share.resource_type === "agent_api_scopes") {
    // Comma-joined scope names, or the server's "no scopes".
    return share.level === "no scopes" ? "No scopes" : share.level
  }
  return NON_CATALOG_LEVELS[share.level] || share.level
}

/**
 * "3 credentials · 2 skills · 1 artifact" from an impact's `shares_by_type`,
 * zero counts left out. Empty string when nothing is counted.
 */
export function sharesByTypeSentence(
  sharesByType: Record<string, number> | undefined,
): string {
  if (!sharesByType) return ""
  return TYPE_ORDER.filter((type) => (sharesByType[type] ?? 0) > 0)
    .map((type) => {
      const n = sharesByType[type]
      const meta = USER_GROUP_SHARE_TYPES[type]
      // Non-breaking inside each part, so "1 AI credential" never wraps
      // mid-part; the line breaks only at the " · " separators.
      return `${n} ${n === 1 ? meta.one : meta.many}`.replace(/ /g, "\u00a0")
    })
    .join(" · ")
}
