/**
 * useRole — derive role-aware predicates from the current user.
 *
 * Phase 3 of the Agent Bundles & Installs plan introduces three roles:
 *   * ``agent-user``      — read-only catalog + conversation
 *   * ``agent-developer`` — full developer UI
 *   * ``admin``           — developer UI + admin nav, paired with ``is_superuser``
 *
 * The frontend shell branches on the role to mount either the
 * simplified ``AgentUserLayout`` or the existing layout.  Component-
 * level UI gating (hiding Bundle tab, prompts editor, etc.) reads
 * ``isDeveloper`` from this hook.
 *
 * Defense-in-depth: API guards block agent-developer-only mutations
 * server-side, but the UI must still hide the affordances so we never
 * advertise something the user can't do.
 */
import useAuth from "@/hooks/useAuth"

export type UserRoleValue = "agent-user" | "agent-developer" | "admin"

export interface UseRoleResult {
  /** Raw role string from the current user (or null while loading). */
  role: UserRoleValue | null
  /** True when role resolves to agent-developer or admin. */
  isDeveloper: boolean
  /** True when role resolves to agent-user. */
  isAgentUser: boolean
  /** True when role resolves to admin (also true for superusers). */
  isAdmin: boolean
  /** True until the current user has been fetched. */
  isLoading: boolean
}

export default function useRole(): UseRoleResult {
  const { user } = useAuth()

  const role = (user?.role as UserRoleValue | undefined) ?? null
  const isAdmin = !!user?.is_superuser || role === "admin"
  const isDeveloper = isAdmin || role === "agent-developer"
  const isAgentUser = !!user && !isDeveloper

  return {
    role,
    isDeveloper,
    isAgentUser,
    isAdmin,
    isLoading: !user,
  }
}
