import { type LucideIcon, ShieldCheck, User, Users } from "lucide-react"

/**
 * One glyph per kind of sharing principal, product-wide (user groups UI spec,
 * Overrides): a person is `User`, a user group is `Users`, a role is
 * `ShieldCheck`. Every picker, pill and grant row reads it from here.
 */
export type SharePrincipalKind = "user" | "group" | "role"

export const PRINCIPAL_KIND_ICON: Record<SharePrincipalKind, LucideIcon> = {
  user: User,
  group: Users,
  role: ShieldCheck,
}
