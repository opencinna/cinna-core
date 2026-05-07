/**
 * useRoleEvents — handle the ``USER_ROLE_CHANGED`` WebSocket event.
 *
 * Mounted once at the protected ``_layout`` root.  When the backend
 * fires the event for the current user (admin promoting / demoting
 * them), this hook:
 *
 * 1. Refetches the ``["currentUser"]`` query so the role-aware shell
 *    re-renders with the new role.
 * 2. If the user is now an ``agent-user`` and is sitting on a route
 *    that's developer-only (anything under ``/agents`` or
 *    ``/agentic-teams``), redirects to ``/catalog``.
 * 3. Surfaces a toast so the change is visible.
 *
 * The backend is the source of truth — guards on every developer
 * endpoint will refuse the action if the role check fails — so the
 * UI redirect is purely a UX nicety.
 */
import { useQueryClient } from "@tanstack/react-query"
import { useNavigate, useRouterState } from "@tanstack/react-router"

import useAuth from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import { useEventSubscription, EventTypes } from "@/hooks/useEventBus"

const DEVELOPER_ONLY_PREFIXES = [
  "/agents",
  "/agent/",
  "/agentic-teams",
  "/admin",
  "/credentials",
  "/credential/",
  "/knowledge-source",
  "/knowledge-sources",
]

export function useRoleEvents() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { user } = useAuth()
  const router = useRouterState()
  const { showSuccessToast } = useCustomToast()

  useEventSubscription(EventTypes.USER_ROLE_CHANGED, (event) => {
    // Only care about events targeted at the current user.  The
    // backend routes role events to ``user_{user_id}`` rooms with
    // both ``meta.user_id`` and ``model_id`` populated; if neither
    // is present we must NOT process the event — fail-closed against
    // a hypothetical broadcast misroute that would otherwise refetch
    // and redirect every connected user.
    const eventUserId = event?.meta?.user_id ?? event?.model_id
    if (!user?.id || !eventUserId || eventUserId !== user.id) {
      return
    }

    const newRole = event?.meta?.new_role
    const previousRole = event?.meta?.previous_role

    // Refetch current user so role-derived UI flips.
    queryClient.invalidateQueries({ queryKey: ["currentUser"] })

    if (newRole === "agent-user" && previousRole !== "agent-user") {
      showSuccessToast("Your developer access was removed by an admin.")
      const path = router.location.pathname
      if (DEVELOPER_ONLY_PREFIXES.some((p) => path.startsWith(p))) {
        navigate({ to: "/catalog" })
      }
    } else if (
      (newRole === "agent-developer" || newRole === "admin") &&
      previousRole === "agent-user"
    ) {
      showSuccessToast("You can now build agents — developer access enabled.")
    }
  })
}
