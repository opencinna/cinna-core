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
