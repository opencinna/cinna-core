import type { LibraryGrantIn, LibraryGrantPublic } from "@/client"
import { userRoleLabel } from "@/utils/userRoles"

/**
 * The words the team library uses for access and for who holds it.
 *
 * One module so the admin page's summary column, the access dialog's rows and
 * any later sharing surface spell "Can get" / "Can share changes" /
 * "Maintainer" the same way. Order is capability order, least to most.
 */
export type LibraryAccess = LibraryGrantIn["access"]
export type LibraryPrincipal = LibraryGrantIn["principal"]

export const LIBRARY_ACCESS_OPTIONS: { value: LibraryAccess; label: string }[] =
  [
    { value: "read", label: "Can get" },
    { value: "write", label: "Can share changes" },
    { value: "maintain", label: "Maintainer" },
  ]

const ACCESS_LABELS: Record<LibraryAccess, string> = Object.fromEntries(
  LIBRARY_ACCESS_OPTIONS.map((o) => [o.value, o.label]),
) as Record<LibraryAccess, string>

export function libraryAccessLabel(access: LibraryAccess): string {
  return ACCESS_LABELS[access] ?? access
}

const ACCESS_RANK: Record<LibraryAccess, number> = {
  maintain: 0,
  write: 1,
  read: 2,
}

export type PrincipalKind = "everyone" | "role" | "user"

export function principalKind(principal: LibraryPrincipal): PrincipalKind {
  if (principal === "everyone") return "everyone"
  return "role" in principal ? "role" : "user"
}

/** Stable identity of a principal inside one agent's grant list. */
export function principalKey(principal: LibraryPrincipal): string {
  if (principal === "everyone") return "everyone"
  return "role" in principal
    ? `role:${principal.role}`
    : `user:${principal.user_id}`
}

/**
 * What a grant's principal is called. Roles and Everyone are labelled here,
 * never from the server's `display_name`; a person falls back with `||`
 * because an absent name arrives as `""`.
 */
export function principalLabel(
  principal: LibraryPrincipal,
  displayName: string | null | undefined,
): string {
  if (principal === "everyone") return "Everyone"
  if ("role" in principal) return userRoleLabel(principal.role)
  return displayName || "Unknown person"
}

const KIND_RANK: Record<PrincipalKind, number> = {
  everyone: 0,
  role: 1,
  user: 2,
}

/** Maintainer → Can share changes → Can get; then Everyone → roles → people; then by name. */
export function compareGrants(
  a: { principal: LibraryPrincipal; access: LibraryAccess; title: string },
  b: { principal: LibraryPrincipal; access: LibraryAccess; title: string },
): number {
  return (
    ACCESS_RANK[a.access] - ACCESS_RANK[b.access] ||
    KIND_RANK[principalKind(a.principal)] -
      KIND_RANK[principalKind(b.principal)] ||
    a.title.localeCompare(b.title)
  )
}

/**
 * "Who can get it", as separate facts: `Everyone`, or the roles by name (≤ 2)
 * and the people as a count with its unit. Every access level includes
 * getting the agent, so every grant counts. Returned as a list so a caller
 * can keep each fact on one line and break only between them.
 */
export function audienceFacts(grants: LibraryGrantPublic[]): string[] {
  if (grants.some((g) => g.principal === "everyone")) return ["Everyone"]
  const roles = grants.flatMap((g) =>
    g.principal !== "everyone" && "role" in g.principal
      ? [g.principal.role]
      : [],
  )
  const people = grants.filter(
    (g) => g.principal !== "everyone" && "user_id" in g.principal,
  ).length
  const parts: string[] = []
  if (roles.length > 0) {
    if (roles.length <= 2) parts.push(...roles.map(userRoleLabel))
    else parts.push(`${roles.length} roles`)
  }
  if (people > 0) parts.push(people === 1 ? "1 person" : `${people} people`)
  return parts
}
