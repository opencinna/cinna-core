import type { UserRoleValue } from "@/hooks/useRole"

/**
 * The one place the three roles are spelled for a reader.
 *
 * The labels were already written out by hand in the users table, the edit-user
 * form and the access-policy card; auto-provisioning adds three more surfaces
 * that name the same roles (a checkbox group, a table column of chips, a
 * role matrix). A role renamed in one of six places and not the other five is
 * the failure this exists to prevent, so new surfaces read from here.
 *
 * Order is the capability order (least to most), which is also the order the
 * users page and the edit-user form present them in.
 *
 * The identifiers are exported alongside the table because centralising only
 * the labels is half a job: a surface that needs one role by name (the access
 * policy card's default-role select, which may offer two of the three) was
 * still writing the wire string by hand, so the values could drift from the
 * table that claims to define them.
 */
export const ROLE_AGENT_USER = "agent-user" satisfies UserRoleValue
export const ROLE_AGENT_DEVELOPER = "agent-developer" satisfies UserRoleValue
export const ROLE_ADMIN = "admin" satisfies UserRoleValue

export const USER_ROLE_OPTIONS: { value: UserRoleValue; label: string }[] = [
  { value: ROLE_AGENT_USER, label: "Agent User" },
  { value: ROLE_AGENT_DEVELOPER, label: "Agent Developer" },
  { value: ROLE_ADMIN, label: "Admin" },
]

const USER_ROLE_LABELS: Record<string, string> = Object.fromEntries(
  USER_ROLE_OPTIONS.map((option) => [option.value, option.label]),
)

/**
 * Display label for a role string.
 *
 * Takes a bare `string` rather than `UserRoleValue` because most callers hold a
 * role that came off the wire: a server that learns a fourth role before this
 * build does should render its identifier, not crash or silently show the
 * wrong name.
 */
export function userRoleLabel(role: string): string {
  return USER_ROLE_LABELS[role] ?? role
}
