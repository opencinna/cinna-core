import { useQuery } from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { GraduationCap } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import { SkillsService } from "@/client"
import { CatalogSectionTabs } from "@/components/Catalog/CatalogSectionTabs"
import {
  type SkillCatalogFilter,
  SkillCatalogFilters,
} from "@/components/Catalog/SkillCatalogFilters"
import { SkillCatalogGrid } from "@/components/Catalog/SkillCatalogGrid"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Skeleton } from "@/components/ui/skeleton"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/catalog/skills/")({
  component: SkillsCatalog,
  head: () => ({
    meta: [{ title: `Skills catalog - ${APP_NAME}` }],
  }),
})

function SkillsCatalog() {
  const { setHeaderContent } = usePageHeader()
  const [filter, setFilter] = useState<SkillCatalogFilter>("all")

  useEffect(() => {
    setHeaderContent(
      <div className="min-w-0">
        <h1 className="text-lg font-semibold truncate">Skills catalog</h1>
        <p className="text-xs text-muted-foreground truncate">
          Reusable skills published on this instance
        </p>
      </div>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["skills-catalog"],
    queryFn: () => SkillsService.listSkillCatalog(),
  })

  const entries = useMemo(() => data?.data ?? [], [data])

  // Client-side, over one fetch: the route deliberately returns everything the
  // caller may see, and the bundle catalog beside it sets the precedent.
  const filtered = useMemo(() => {
    switch (filter) {
      case "public":
        return entries.filter((e) => e.visibility === "public")
      case "mine":
        // `can_manage` is the publisher capability the server already computes;
        // comparing publisher ids in the client would be the same rule written
        // twice, and the second copy is the one that goes stale.
        return entries.filter((e) => e.can_manage)
      case "installed":
        return entries.filter(
          (e) => (e.installed_in_agent_ids?.length ?? 0) > 0,
        )
      default:
        return entries
    }
  }, [entries, filter])

  return (
    <div className="p-6 md:p-8 overflow-y-auto space-y-6">
      <div className="mx-auto max-w-7xl space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <CatalogSectionTabs />
          <SkillCatalogFilters value={filter} onChange={setFilter} />
        </div>

        {/* `isError` is gated on there being no data, so a failed background
            refetch keeps the grid on screen instead of blanking the page; a
            first failed load still renders as a failure, never as "nothing
            published yet". */}
        {isError && !data ? (
          <QueryErrorAlert
            error={error}
            fallback="Couldn't load the skills catalog"
            onRetry={() => refetch()}
          />
        ) : isLoading ? (
          // Skeletons shaped like the real rows (§6): the sibling bundle route
          // still shows a four-column `<Table>` of `PendingItems` in front of a
          // card grid, which promises a shape the page never renders.
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 auto-rows-fr">
            {Array.from({ length: 8 }, (_, i) => (
              <Skeleton key={i} className="h-[240px] w-full rounded-xl" />
            ))}
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex flex-col items-center justify-center text-center py-16">
            <div className="rounded-full bg-muted p-4 mb-4">
              <GraduationCap className="h-8 w-8 text-muted-foreground" />
            </div>
            <h3 className="text-lg font-semibold">
              {entries.length === 0
                ? "No skills published yet"
                : "No skills match this filter"}
            </h3>
            <p className="text-muted-foreground max-w-md">
              {entries.length === 0 ? (
                <>
                  {/* A sentence that names another page links to it (§2
                      "Empty state") — the same rule the agent-page Skills card
                      already follows for its own empty copy. */}
                  Publish a skill from{" "}
                  <Link to="/agents" className="text-primary hover:underline">
                    an agent's Configuration tab
                  </Link>{" "}
                  to see it here.
                </>
              ) : (
                "Try a different filter — there are skills in the catalog you don't see in this view."
              )}
            </p>
          </div>
        ) : (
          <SkillCatalogGrid entries={filtered} />
        )}
      </div>
    </div>
  )
}
