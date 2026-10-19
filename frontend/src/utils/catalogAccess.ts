import type { CatalogGrant, CatalogGrantIn } from "@/client"
import type { CatalogKind } from "@/utils/catalogItems"
import { userRoleLabel } from "@/utils/userRoles"

/**
 * The words the catalog uses for access and for who holds it — one module so
 * the People dialog's rows and the admin pages spell "Can use" / "Maintainer"
 * the same way. Order is capability order, least to most. Rebuilt from the
 * team library's `libraryAccess.ts` (d5bb0a16); the Everyone principal is
 * gone, because that is General access now.
 */
export type CatalogAccess = CatalogGrantIn["access"]
export type CatalogPrincipal = CatalogGrantIn["principal"]

export interface CatalogAccessOption {
  value: CatalogAccess
  label: string
}

/**
 * What `edit` lets a holder do, which differs by kind: a desktop agent's
 * holder pushes changes over git, a skill package's holder publishes a new
 * version into the shared package, and a Cinna-hosted bundle's holder only
 * ever gets to use it at any visibility (publishing stays with the publisher).
 */
const EDIT_LABEL: Record<CatalogKind, string> = {
  desktop: "Can share changes",
  skill: "Can publish versions",
  cloud: "Can use (any visibility)",
}

/**
 * The access picker's options for one item kind. A Cinna-hosted bundle does
 * not offer `edit` (it adds nothing a person can act on), but a row that
 * already holds it still shows its value.
 */
export function catalogAccessOptions(
  kind: CatalogKind,
  current?: CatalogAccess,
): CatalogAccessOption[] {
  const offerEdit = kind !== "cloud" || current === "edit"
  return [
    { value: "use", label: "Can use" },
    ...(offerEdit ? [{ value: "edit" as const, label: EDIT_LABEL[kind] }] : []),
    { value: "maintain", label: "Maintainer" },
  ]
}

/** One level's words for one kind; an unknown level is shown as sent. */
export function catalogAccessLabel(kind: CatalogKind, access: string): string {
  if (access === "use") return "Can use"
  if (access === "maintain") return "Maintainer"
  if (access === "edit") return EDIT_LABEL[kind]
  return access
}

const ACCESS_RANK: Record<CatalogAccess, number> = {
  maintain: 0,
  edit: 1,
  use: 2,
}

export type PrincipalKind = "role" | "group" | "user"

export function principalKind(principal: CatalogPrincipal): PrincipalKind {
  if ("role" in principal) return "role"
  if ("group_id" in principal) return "group"
  return "user"
}

/** Stable identity of a principal inside one item's grant list. */
export function principalKey(principal: CatalogPrincipal): string {
  if ("role" in principal) return `role:${principal.role}`
  if ("group_id" in principal) return `group:${principal.group_id}`
  return `user:${principal.user_id}`
}

/**
 * What a grant's principal is called. Roles are labelled here, never from the
 * server's `display_name`; a person falls back with `||` because an absent
 * name arrives as `""`.
 */
export function principalLabel(
  principal: CatalogPrincipal,
  displayName: string | null | undefined,
): string {
  if ("role" in principal) return userRoleLabel(principal.role)
  if ("group_id" in principal) return displayName || "Unknown group"
  return displayName || "Unknown person"
}

const KIND_RANK: Record<PrincipalKind, number> = { role: 0, group: 1, user: 2 }

/**
 * Maintainer → edit → Can use; then roles → groups → people;
 * then by name.
 */
export function compareGrants(
  a: { principal: CatalogPrincipal; access: CatalogAccess; title: string },
  b: { principal: CatalogPrincipal; access: CatalogAccess; title: string },
): number {
  return (
    ACCESS_RANK[a.access] - ACCESS_RANK[b.access] ||
    KIND_RANK[principalKind(a.principal)] -
      KIND_RANK[principalKind(b.principal)] ||
    a.title.localeCompare(b.title)
  )
}

/** One grant as the People dialog holds it before Save. */
export interface BufferedGrant {
  key: string
  principal: CatalogPrincipal
  access: CatalogAccess
  /** The level the server holds, before any edit here (`undefined` = new row). */
  savedAccess?: CatalogAccess
  title: string
  /** A group's member count, for the row's detail flag. */
  memberCount?: number | null
}

export function toBufferedGrants(grants: CatalogGrant[]): BufferedGrant[] {
  return grants
    .map((g) => ({
      key: principalKey(g.principal),
      principal: g.principal,
      access: g.access,
      savedAccess: g.access,
      title: principalLabel(g.principal, g.display_name),
      memberCount: g.member_count,
    }))
    .sort(compareGrants)
}
