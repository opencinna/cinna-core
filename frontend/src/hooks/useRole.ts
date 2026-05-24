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
import { createContext, useContext } from "react"

import useAuth from "@/hooks/useAuth"

export type UserRoleValue = "agent-user" | "agent-developer" | "admin"

/**
 * Subtree-scoped capability override.
 *
 * Some surfaces must present at the ``agent-user`` capability level
 * regardless of the viewer's real role. The canonical case is a
 * consumer / foreign **bundle install**: installing a bundle grants
 * use-only capabilities — never build capabilities — even for
 * agent-developers and admins (you install an agent to *use* it, you
 * don't build someone else's bundle).
 *
 * The agent detail page wraps its tab content in this provider with
 * ``forceAgentUser`` set for foreign installs; every ``useRole()`` call
 * underneath then degrades to the agent-user view, so any role-gated
 * sub-control (current or future) is hidden automatically without
 * threading flags through each component.
 */
export interface RoleOverride {
  forceAgentUser: boolean
}

export const RoleOverrideContext = createContext<RoleOverride>({
  forceAgentUser: false,
})

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
  const { forceAgentUser } = useContext(RoleOverrideContext)

  const role = (user?.role as UserRoleValue | undefined) ?? null

  // A subtree may force the capability level down to agent-user (e.g. a
  // foreign bundle install). The raw ``role`` is still surfaced for
  // display, but all capability predicates degrade.
  if (forceAgentUser) {
    return {
      role,
      isDeveloper: false,
      isAgentUser: !!user,
      isAdmin: false,
      isLoading: !user,
    }
  }

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
