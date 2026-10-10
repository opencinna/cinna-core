import { useQuery } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"
import { ArrowLeft } from "lucide-react"
import { useEffect, useState } from "react"

import { CatalogService } from "@/client"
import { DesktopAgentCard } from "@/components/Catalog/DesktopAgent/DesktopAgentCard"
import { ReleaseNotesCard } from "@/components/Catalog/DesktopAgent/ReleaseNotesCard"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { useNavigationHistory } from "@/hooks/useNavigationHistory"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"
import { catalogItemQueryKey, catalogKindOf } from "@/utils/catalogItems"

export const Route = createFileRoute("/_layout/catalog/items/$uuid")({
  component: CatalogItemRoute,
  head: () => ({
    meta: [{ title: `Agent - Catalog - ${APP_NAME}` }],
  }),
})

/**
 * S2 — a desktop agent before the reader gets it on their computer. Serves
 * `runs_on='desktop'` only: any other kind redirects to its own detail page,
 * so one entity has one page. Same host shape as the skill package route.
 */
function CatalogItemRoute() {
  const { uuid } = Route.useParams()
  const { setHeaderContent } = usePageHeader()
  const { goBack } = useNavigationHistory()
  const navigate = useNavigate()
  // `null` = the latest version; a pin comes from the Versions Sheet.
  const [pinnedRevision, setPinnedRevision] = useState<number | null>(null)

  const {
    data: item,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: catalogItemQueryKey(uuid),
    queryFn: () => CatalogService.getCatalogItem({ itemUuid: uuid }),
  })

  useEffect(() => {
    if (!item) return
    const kind = catalogKindOf(item)
    if (kind === "skill") {
      navigate({
        to: "/catalog/skills/$packageId",
        params: { packageId: item.uuid },
        replace: true,
      })
    } else if (kind === "cloud") {
      navigate({
        to: "/catalog/agents/install/$bundleId",
        params: { bundleId: item.id },
        replace: true,
      })
    }
  }, [item, navigate])

  useEffect(() => {
    setHeaderContent(
      <div className="flex min-w-0 items-center gap-3">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => goBack("/catalog")}
          className="shrink-0"
          aria-label="Back"
        >
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div className="min-w-0">
          <h1 className="truncate text-base font-semibold">
            {item?.display_name || "Agent"}
          </h1>
          <p className="truncate text-xs text-muted-foreground">
            {item?.publisher?.display_name
              ? `by ${item.publisher.display_name}`
              : "Catalog"}
          </p>
        </div>
      </div>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, item, goBack])

  if (isError && !item) {
    return (
      <div className="overflow-y-auto p-6 md:p-8">
        <div className="mx-auto max-w-6xl space-y-4">
          <QueryErrorAlert
            error={error}
            fallback="Couldn't load this agent"
            onRetry={() => refetch()}
          >
            {/* A 404 is also what "you may not see this" looks like. */}
            <p className="text-xs">
              If it was made private or deleted, it is no longer in the{" "}
              <Link
                to="/catalog"
                search={{ kind: "desktop" }}
                className="underline"
              >
                catalog
              </Link>
              .
            </p>
          </QueryErrorAlert>
        </div>
      </div>
    )
  }

  const revisions = item?.revisions ?? []
  const shownNumber = pinnedRevision ?? item?.latest?.revision_number ?? null
  const shownRevision =
    revisions.find((r) => r.revision_number === shownNumber) ?? null
  const ready = !isLoading && item && catalogKindOf(item) === "desktop"

  return (
    <div className="overflow-y-auto p-6 md:p-8">
      <div className="mx-auto max-w-6xl">
        {/* A detail route, not a card grid: the asymmetric split is the
            host's shape (the skill package route, the install page). */}
        <div className="grid items-start gap-6 lg:grid-cols-[minmax(280px,360px)_1fr]">
          {!ready ? (
            <>
              <Skeleton className="h-[340px] w-full rounded-xl" />
              <Skeleton className="h-[320px] w-full rounded-xl" />
            </>
          ) : (
            <>
              <DesktopAgentCard
                item={item}
                shownRevision={shownRevision}
                onSelectRevision={setPinnedRevision}
              />
              <ReleaseNotesCard revision={shownRevision} />
            </>
          )}
        </div>
      </div>
    </div>
  )
}
