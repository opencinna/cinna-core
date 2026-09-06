import { useQuery } from "@tanstack/react-query"

import { type LandingPagePublic, ServerConfigService } from "@/client"

/**
 * The admin-authored welcome copy shown on the public `/start` page.
 *
 * A separate query from `useAccessPolicy` because it is separate *content*:
 * `["accessPolicy"]` is read on every login and signup page load, and none of
 * them render this. Folding the markdown into that projection would put a
 * landing page on the wire for every login view — which is why the backend
 * serves it from `GET /server-config/landing` instead.
 *
 * Same anonymous rate-limit bucket as the policy read on the backend, so the
 * same rule applies here: `retry: false`, and the caller must render something
 * sensible when `data` is undefined. On `/start` that is simply nothing — an
 * absent welcome message and an unread one look identical, and both are fine.
 */
export function useLandingPage() {
  return useQuery<LandingPagePublic>({
    queryKey: ["landingPage"],
    queryFn: () => ServerConfigService.getLandingPage(),
    staleTime: 60_000,
    retry: false,
  })
}
