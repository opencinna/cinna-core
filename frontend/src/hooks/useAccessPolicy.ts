import { useQuery } from "@tanstack/react-query"

import { type AccessPolicyPublic, ServerConfigService } from "@/client"

/**
 * The instance's front-door policy, as anyone may read it.
 *
 * `GET /server-config/access-policy` is unauthenticated on purpose: the login
 * and signup pages have to render the right front door *before* anyone has a
 * token. It says what the instance **offers** — never what a particular person
 * may do — so there is nothing here to gate on a viewer.
 *
 * One shared key with a 60s `staleTime`, so the several surfaces that ask
 * (login, signup, the admin card) cost one request per minute. `retry: false`
 * because the endpoint is IP rate-limited: hammering it after a 429 is the one
 * thing that would keep it failing. Every caller must therefore degrade
 * gracefully when `data` is undefined — the backend enforces the policy
 * regardless of what the UI managed to render.
 */
export function useAccessPolicy() {
  return useQuery<AccessPolicyPublic>({
    queryKey: ["accessPolicy"],
    queryFn: () => ServerConfigService.getAccessPolicy(),
    staleTime: 60_000,
    retry: false,
  })
}

/**
 * Whether a Google sign-in button can actually appear on this deployment.
 *
 * Two independently configured facts, and both have to hold: the backend's
 * client id and secret (which the projection reports as `google_auth_enabled`)
 * and this frontend build's own `VITE_GOOGLE_CLIENT_ID`, without which
 * `GoogleLoginButton` renders nothing at all. Reading only one of them is how
 * a page ends up describing a button that was never rendered — which is why
 * the answer is computed here once instead of in each page that asks.
 *
 * `undefined` in, `undefined` out, deliberately: the callers do not share a
 * degradation. `/login` and `/signup` are the front door and degrade
 * permissively (`?? true`) — a wrong guess there costs one rejected request.
 * `/start` says nothing until it knows (`=== true`), because it is a hub and
 * silence is available to it.
 *
 * Structurally typed rather than taking `AccessPolicyPublic`, because the
 * same fact reaches `/accept-invite` on a different projection
 * (`InvitationLookupPublic.google_auth_enabled`). Narrowing this to one
 * response model is what left that page with a fourth hand-rolled copy.
 */
export function googleSignInAvailable(
  source: { google_auth_enabled?: boolean | null } | undefined,
): boolean | undefined {
  if (!import.meta.env.VITE_GOOGLE_CLIENT_ID) return false
  return source?.google_auth_enabled ?? undefined
}
