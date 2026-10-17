import { type QueryClient, useQuery } from "@tanstack/react-query"

import {
  type ArtifactPublic,
  ArtifactsService,
  type ArtifactViewPublic,
} from "@/client"
import { parseTimestamp } from "@/components/Common/RelativeTime"

export type ArtifactScope = "mine" | "shared_with_me"

const PAGE_SIZE = 100

export const artifactKeys = {
  all: ["artifacts"] as const,
  list: (scope: ArtifactScope, workspace?: string) =>
    scope === "mine"
      ? (["artifacts", "mine", workspace ?? null, "all-pages"] as const)
      : (["artifacts", "shared_with_me", "all-pages"] as const),
  detail: (id: string) => ["artifact", id] as const,
  view: (id: string, rev: number | null) =>
    ["artifactView", id, rev ?? "current"] as const,
  capabilities: ["artifactsCapabilities"] as const,
}

/** The key a delete runs under, so a list row can show it as pending. */
export function deleteArtifactMutationKey(id: string) {
  return ["delete-artifact", id] as const
}

/**
 * Every artifact in one scope, page by page (the credentials-table lesson: a
 * single fixed-size read silently drops what is past the first page). Rows are
 * keyed by id so one that shifts between two page reads is listed once; the
 * server orders by `updated_at desc, id`, which this keeps.
 */
export async function readAllArtifacts(
  scope: ArtifactScope,
  workspaceId: string | undefined,
): Promise<ArtifactPublic[]> {
  const byId = new Map<string, ArtifactPublic>()
  let skip = 0
  for (;;) {
    const page = await ArtifactsService.listArtifacts({
      scope,
      skip,
      limit: PAGE_SIZE,
      userWorkspaceId: scope === "mine" ? workspaceId : undefined,
    })
    for (const artifact of page.data) byId.set(artifact.id, artifact)
    skip += page.data.length
    if (page.data.length === 0 || skip >= page.count) break
  }
  return [...byId.values()]
}

/**
 * Everything an artifact change can make stale: the lists and the detail, and
 * — only when what is *shown* changed (the start page) — its views. A view
 * refetch mints a new capability, which reloads a site the user is in.
 */
export function invalidateArtifact(
  queryClient: QueryClient,
  id?: string,
  { views = false }: { views?: boolean } = {},
) {
  const pending = [
    queryClient.invalidateQueries({ queryKey: artifactKeys.all }),
  ]
  if (id) {
    pending.push(
      queryClient.invalidateQueries({ queryKey: artifactKeys.detail(id) }),
    )
    if (views) {
      pending.push(
        queryClient.invalidateQueries({ queryKey: ["artifactView", id] }),
      )
    }
  }
  return Promise.all(pending)
}

/** Refresh a view capability this long before it expires. */
const REFRESH_LEAD_MS = 5 * 60 * 1000

/**
 * When a view (a capability minted for one revision) needs re-minting: a few
 * minutes before `expires_at`, never sooner than a minute from now.
 */
export function viewRefreshInterval(
  view: ArtifactViewPublic | undefined,
): number | false {
  if (!view) return false
  const expires = parseTimestamp(view.expires_at)
  if (!expires) return false
  return Math.max(expires.getTime() - Date.now() - REFRESH_LEAD_MS, 60_000)
}

/** The signed-in view of one revision (`rev` null = the current one). */
export function useArtifactView(
  artifactId: string,
  rev: number | null,
  enabled = true,
) {
  return useQuery({
    queryKey: artifactKeys.view(artifactId, rev),
    queryFn: () =>
      ArtifactsService.viewArtifact({ artifactId, rev: rev ?? undefined }),
    enabled,
    // The cap is re-minted silently before it lapses (plan §4); a focus
    // refetch would re-mint for nothing and reload a site mid-use.
    refetchInterval: (query) => viewRefreshInterval(query.state.data),
    refetchOnWindowFocus: false,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  })
}

/** Upload limits for the client-side pre-check. */
export function useArtifactCapabilities() {
  return useQuery({
    queryKey: artifactKeys.capabilities,
    queryFn: () => ArtifactsService.getArtifactsCapabilities(),
    staleTime: 10 * 60 * 1000,
  })
}
