import type { CatalogGrant, CatalogGrantIn } from "@/client"
import { userRoleLabel } from "@/utils/userRoles"

/**
 * The words the catalog uses for access and for who holds it — one module so
 * the People dialog's rows and the admin table spell "Can use" / "Can share
 * changes" / "Maintainer" the same way. Order is capability order, least to
 * most. Rebuilt from the team library's `libraryAccess.ts` (d5bb0a16); the
 * Everyone principal is gone, because that is General access now.
 */
export type CatalogAccess = CatalogGrantIn["access"]
export type CatalogPrincipal = CatalogGrantIn["principal"]

export const CATALOG_ACCESS_OPTIONS: { value: CatalogAccess; label: string }[] =
  [
    { value: "use", label: "Can use" },
    { value: "edit", label: "Can share changes" },
    { value: "maintain", label: "Maintainer" },
  ]

const ACCESS_RANK: Record<CatalogAccess, number> = {
  maintain: 0,
  edit: 1,
  use: 2,
}

export type PrincipalKind = "role" | "user"

export function principalKind(principal: CatalogPrincipal): PrincipalKind {
  return "role" in principal ? "role" : "user"
}

/** Stable identity of a principal inside one item's grant list. */
export function principalKey(principal: CatalogPrincipal): string {
  return "role" in principal
    ? `role:${principal.role}`
    : `user:${principal.user_id}`
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
  return displayName || "Unknown person"
}

const KIND_RANK: Record<PrincipalKind, number> = { role: 0, user: 1 }

/** Maintainer → Can share changes → Can use; then roles → people; then by name. */
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
  title: string
}

export function toBufferedGrants(grants: CatalogGrant[]): BufferedGrant[] {
  return grants
    .map((g) => ({
      key: principalKey(g.principal),
      principal: g.principal,
      access: g.access,
      title: principalLabel(g.principal, g.display_name),
    }))
    .sort(compareGrants)
}
