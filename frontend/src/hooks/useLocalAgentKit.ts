import { useQuery } from "@tanstack/react-query"

// Falls back to "" (same origin), matching `OpenAPI.BASE` in `main.tsx` and the
// other hand-rolled API callers in `utils.ts`. Reading `OpenAPI.BASE` directly
// would inherit its `undefined` on a same-origin deployment where VITE_API_URL
// is unset, producing a literal "undefined/api/agent-start/version" request.
const API_BASE_URL = import.meta.env.VITE_API_URL || ""

interface LocalAgentKitVersion {
  kit_version: string
  schema_version: number
  platform_url: string
  kit_base_url: string
  start_url: string
  instance_name: string
}

/**
 * Whether this instance publishes the public Local Agent Kit surface.
 *
 * Every surface that tells a user to paste `read <host>/agent-start …` has to ask,
 * including the ones behind login: an admin can switch the surface off
 * (`ServerConfig.local_agent_kit_enabled`), and a hint pointing at a URL that
 * 404s is worse than no hint. `GET /admin/server-config` is superuser-only, so
 * the flag cannot be read that way from a normal user's session — and on the
 * login page there is no session at all.
 *
 * The probe is therefore the kit's own public endpoint, which 404s on an
 * instance that opted out. It is excluded from the OpenAPI schema (it serves
 * static content to strangers, and is not part of the API contract), so there
 * is no generated client method for it and a plain `fetch` is the honest way to
 * ask. `/api/agent-start`, not `/agent-start`: the alias every deployment already proxies,
 * so a proxy missing the pretty-URL block does not hide a working feature.
 *
 * One shared query key and an infinite `staleTime`, so the several call sites
 * cost one request per session. Failures are silent by design — this only
 * decides whether an optional pointer is shown.
 */
export function useLocalAgentKitAvailable(): boolean {
  const { data } = useQuery<LocalAgentKitVersion>({
    queryKey: ["localAgentKitVersion"],
    queryFn: async () => {
      const response = await fetch(`${API_BASE_URL}/api/agent-start/version`)
      if (!response.ok) {
        throw new Error(`local agent kit probe failed: ${response.status}`)
      }
      return (await response.json()) as LocalAgentKitVersion
    },
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  })
  return Boolean(data?.kit_version)
}
