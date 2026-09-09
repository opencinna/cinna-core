import { useQuery } from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { ArrowLeft } from "lucide-react"
import { useEffect, useState } from "react"

import { SkillsService } from "@/client"
import { SkillPackageAccessCard } from "@/components/Catalog/SkillPackageAccessCard"
import { SkillPackageCard } from "@/components/Catalog/SkillPackageCard"
import { SkillRevisionsCard } from "@/components/Catalog/SkillRevisionsCard"
import { SkillSourceCard } from "@/components/Catalog/SkillSourceCard"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { useNavigationHistory } from "@/hooks/useNavigationHistory"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"
import { skillPublisherLabel, skillRevisionLabel } from "@/utils/skillCatalog"

export const Route = createFileRoute("/_layout/catalog/skills/$packageId")({
  component: SkillPackageRoute,
  head: () => ({
    meta: [{ title: `Skill package - ${APP_NAME}` }],
  }),
})

function SkillPackageRoute() {
  const { packageId } = Route.useParams()
  const { setHeaderContent } = usePageHeader()
  const { goBack } = useNavigationHistory()

  // Which revision the SKILL.md panel shows. `null` means "whatever the
  // package says is latest" — resolved below rather than seeded from an effect,
  // so the panel never flashes the wrong revision while the query lands.
  const [pinnedRevision, setPinnedRevision] = useState<number | null>(null)

  const {
    data: pkg,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: ["skills-catalog", "package", packageId],
    queryFn: () => SkillsService.getSkillPackage({ packageId }),
  })

  useEffect(() => {
    setHeaderContent(
      <div className="flex items-center gap-3 min-w-0">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => goBack("/catalog/skills")}
          className="shrink-0"
        >
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div className="min-w-0">
          <h1 className="text-base font-semibold truncate">
            {pkg?.display_name ?? "Skill package"}
          </h1>
          <p className="text-xs text-muted-foreground truncate">
            {pkg ? `by ${skillPublisherLabel(pkg)}` : "Skills catalog"}
          </p>
        </div>
      </div>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, pkg, goBack])

  const revisions = pkg?.revisions ?? []
  const selectedRevisionNumber =
    pinnedRevision ?? pkg?.latest_revision_number ?? null
  const selectedRevision = revisions.find(
    (rev) => rev.revision_number === selectedRevisionNumber,
  )

  if (isError && !pkg) {
    return (
      <div className="p-6 md:p-8 overflow-y-auto">
        <div className="mx-auto max-w-6xl space-y-4">
          <QueryErrorAlert
            error={error}
            fallback="Couldn't load this package"
            onRetry={() => refetch()}
          >
            {/* A 404 here is also what "you may not see this package" looks
                like — visibility is enforced by the same lookup — so the way
                out is named rather than left to the browser's Back button. */}
            <p className="text-xs">
              If it was delisted or made private, it is no longer in the{" "}
              <Link to="/catalog/skills" className="underline">
                skills catalog
              </Link>
              .
            </p>
          </QueryErrorAlert>
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-6xl">
        {/* A detail route, not a card grid: the asymmetric split is the host's
            shape (the same one `Install/InstallPage` uses), so A8 — which is
            about a card spanning two columns of a card grid — does not apply. */}
        <div className="grid items-start gap-6 lg:grid-cols-[minmax(280px,360px)_1fr]">
          {isLoading || !pkg ? (
            <>
              <Skeleton className="h-[340px] w-full rounded-xl" />
              <div className="space-y-6">
                <Skeleton className="h-[320px] w-full rounded-xl" />
                <Skeleton className="h-[240px] w-full rounded-xl" />
              </div>
            </>
          ) : (
            <>
              <div className="lg:order-1">
                <SkillPackageCard pkg={pkg} />
              </div>
              <div className="lg:order-2 space-y-6">
                <SkillSourceCard
                  packageId={packageId}
                  revisionNumber={selectedRevisionNumber}
                  revisionLabel={
                    selectedRevision
                      ? skillRevisionLabel(selectedRevision)
                      : null
                  }
                />
                <SkillRevisionsCard
                  revisions={revisions}
                  latestRevisionId={pkg.latest_revision_id}
                  selectedRevisionNumber={selectedRevisionNumber}
                  onView={setPinnedRevision}
                />
                {/* Last in the column, and only for the publisher: a card that
                    appears and disappears with `visibility` must sit at the
                    end or it reflows everything under it each time it is
                    toggled. It self-hides — see the card. */}
                <SkillPackageAccessCard pkg={pkg} />
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
