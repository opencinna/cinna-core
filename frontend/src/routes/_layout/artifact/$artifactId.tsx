import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useCallback, useEffect, useState } from "react"

import { ApiError, ArtifactsService } from "@/client"
import { artifactTitle } from "@/components/Artifacts/artifactFormat"
import {
  artifactKeys,
  invalidateArtifact,
  useArtifactView,
} from "@/components/Artifacts/artifactQueries"
import { DeleteArtifactDialog } from "@/components/Artifacts/DeleteArtifactDialog"
import { EditArtifactDialog } from "@/components/Artifacts/EditArtifactDialog"
import { AllRevisionsSheet } from "@/components/Artifacts/Page/AllRevisionsSheet"
import {
  ArtifactDetailsPanel,
  ArtifactDetailsPanelSkeleton,
} from "@/components/Artifacts/Page/ArtifactDetailsPanel"
import { ArtifactPageHeader } from "@/components/Artifacts/Page/ArtifactPageHeader"
import { ArtifactShareDialog } from "@/components/Artifacts/Share/ArtifactShareDialog"
import {
  ArtifactViewer,
  fullScreenUrl,
} from "@/components/Artifacts/viewers/ArtifactViewer"
import { ViewerSkeleton } from "@/components/Artifacts/viewers/ViewerStates"
import NotFound from "@/components/Common/NotFound"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME, getErrorMessage } from "@/utils"

interface ArtifactSearch {
  /** The file shown in a files artifact. */
  file?: string
  /** An old revision on screen (owner); absent = the current one. */
  rev?: number
}

export const Route = createFileRoute("/_layout/artifact/$artifactId")({
  component: ArtifactPage,
  validateSearch: (search: Record<string, unknown>): ArtifactSearch => {
    const rev = Number(search.rev)
    return {
      file: typeof search.file === "string" ? search.file : undefined,
      rev: Number.isInteger(rev) && rev > 0 ? rev : undefined,
    }
  },
  head: () => ({ meta: [{ title: `Artifact - ${APP_NAME}` }] }),
})

const DETAILS_KEY = "artifact-details-open"
const XL_QUERY = "(min-width: 1280px)"

/** Inline panel at `xl` and up; a Sheet below (where the viewer needs the width). */
function useIsXl() {
  const [isXl, setIsXl] = useState(() => window.matchMedia(XL_QUERY).matches)
  useEffect(() => {
    const mql = window.matchMedia(XL_QUERY)
    const onChange = () => setIsXl(mql.matches)
    mql.addEventListener("change", onChange)
    return () => mql.removeEventListener("change", onChange)
  }, [])
  return isXl
}

function ArtifactPage() {
  const { artifactId } = Route.useParams()
  const search = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })
  const queryClient = useQueryClient()
  const { setHeaderContent } = usePageHeader()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const isXl = useIsXl()

  const [panelOpen, setPanelOpen] = useState(
    () => localStorage.getItem(DETAILS_KEY) !== "false",
  )
  const [sheetOpen, setSheetOpen] = useState(false)
  const [shareOpen, setShareOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [allRevisionsOpen, setAllRevisionsOpen] = useState(false)

  const detail = useQuery({
    queryKey: artifactKeys.detail(artifactId),
    queryFn: () => ArtifactsService.getArtifact({ artifactId }),
    retry: (count, err) =>
      !(err instanceof ApiError && [403, 404].includes(err.status)) &&
      count < 2,
  })
  const artifact = detail.data
  const current = artifact?.current_revision_number ?? null
  const oldRevision =
    search.rev !== undefined && search.rev !== current ? search.rev : null
  // With `?rev=` the detail decides whether that is the current revision.
  const view = useArtifactView(
    artifactId,
    oldRevision,
    search.rev === undefined || artifact !== undefined,
  )

  const selectFile = useCallback(
    (path: string) =>
      navigate({ search: (prev: ArtifactSearch) => ({ ...prev, file: path }) }),
    [navigate],
  )
  const viewRevision = useCallback(
    (number: number) =>
      navigate({
        search: {
          rev: number === current ? undefined : number,
          file: undefined,
        },
      }),
    [navigate, current],
  )

  const openAsSite = useMutation({
    mutationFn: (path: string) =>
      ArtifactsService.updateArtifact({
        artifactId,
        requestBody: { entry_path: path },
      }),
    onSuccess: (_, path) => {
      showSuccessToast(`${path} now opens as the site`)
      navigate({ search: { rev: undefined, file: undefined } })
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Couldn't open it as the site")),
    onSettled: () =>
      invalidateArtifact(queryClient, artifactId, { views: true }),
  })

  const detailsOpen = isXl ? panelOpen : sheetOpen
  const toggleDetails = useCallback(() => {
    if (isXl) {
      setPanelOpen((open) => {
        localStorage.setItem(DETAILS_KEY, String(!open))
        return !open
      })
    } else {
      setSheetOpen((open) => !open)
    }
  }, [isXl])

  const viewData = view.data
  useEffect(() => {
    if (!artifact) {
      setHeaderContent(
        detail.isPending ? <Skeleton className="h-5 w-48" /> : null,
      )
      return () => setHeaderContent(null)
    }
    setHeaderContent(
      <ArtifactPageHeader
        artifact={artifact}
        oldRevision={oldRevision}
        onShowCurrent={() => viewRevision(current ?? 0)}
        downloadHref={viewData?.download_url ?? null}
        fullScreenHref={viewData ? fullScreenUrl(viewData, search.file) : null}
        detailsOpen={detailsOpen}
        onToggleDetails={toggleDetails}
        onShare={() => setShareOpen(true)}
        onEdit={() => setEditOpen(true)}
        onDelete={() => setDeleteOpen(true)}
      />,
    )
    return () => setHeaderContent(null)
  }, [
    artifact,
    detail.isPending,
    oldRevision,
    current,
    viewData,
    search.file,
    detailsOpen,
    toggleDetails,
    viewRevision,
    setHeaderContent,
  ])

  if (detail.isError && !artifact) {
    if (
      detail.error instanceof ApiError &&
      [403, 404].includes(detail.error.status)
    ) {
      return (
        <NotFound
          inline
          title="Artifact not found"
          message="This artifact doesn't exist or you can't open it."
          fallbackPath="/artifacts"
        />
      )
    }
    return (
      <div className="p-6 md:p-8">
        <QueryErrorAlert
          error={detail.error}
          fallback="Couldn't load this artifact."
          onRetry={() => detail.refetch()}
        />
      </div>
    )
  }

  const panel = artifact ? (
    <ArtifactDetailsPanel
      artifact={artifact}
      viewedNumber={oldRevision}
      onViewRevision={(n) => {
        viewRevision(n)
        setSheetOpen(false)
      }}
      // Below `xl` the panel is a Sheet: it closes before what it opens
      // appears, so nothing stacks on it (§2 Disclosure depth).
      onEdit={() => {
        setSheetOpen(false)
        setEditOpen(true)
      }}
      onShare={() => {
        setSheetOpen(false)
        setShareOpen(true)
      }}
      onShowAllRevisions={() => {
        setSheetOpen(false)
        setAllRevisionsOpen(true)
      }}
    />
  ) : (
    <ArtifactDetailsPanelSkeleton />
  )

  return (
    <div className="flex min-h-0 flex-1">
      <div className="min-h-0 min-w-0 flex-1">
        {artifact ? (
          <ArtifactViewer
            title={artifactTitle(artifact)}
            view={viewData}
            isError={view.isError}
            onRetry={() => view.refetch()}
            selectedFile={search.file}
            onSelectFile={selectFile}
            openAsSite={
              artifact.is_owner && oldRevision === null
                ? {
                    onOpenAsSite: (path) => openAsSite.mutate(path),
                    pending: openAsSite.isPending,
                  }
                : undefined
            }
          />
        ) : (
          <ViewerSkeleton />
        )}
      </div>

      {isXl && panelOpen && (
        <aside
          aria-label="Details"
          className="w-80 shrink-0 overflow-y-auto border-l"
        >
          {panel}
        </aside>
      )}
      {!isXl && (
        <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
          <SheetContent side="right" className="w-full sm:max-w-sm">
            <SheetHeader>
              <SheetTitle>Details</SheetTitle>
              <SheetDescription className="break-words">
                {artifact ? artifactTitle(artifact) : ""}
              </SheetDescription>
            </SheetHeader>
            <div className="flex-1 overflow-y-auto">{panel}</div>
          </SheetContent>
        </Sheet>
      )}

      {artifact?.is_owner && (
        <>
          <ArtifactShareDialog
            artifact={
              shareOpen ? { id: artifact.id, title: artifact.title } : null
            }
            onClose={() => setShareOpen(false)}
          />
          <AllRevisionsSheet
            artifactId={artifact.id}
            artifactTitle={artifactTitle(artifact)}
            revisions={[...(artifact.revisions ?? [])].sort(
              (a, b) => b.number - a.number,
            )}
            viewedNumber={oldRevision ?? current}
            onView={viewRevision}
            open={allRevisionsOpen}
            onOpenChange={setAllRevisionsOpen}
          />
          <EditArtifactDialog
            artifact={artifact}
            open={editOpen}
            onOpenChange={setEditOpen}
          />
          <DeleteArtifactDialog
            artifact={deleteOpen ? artifact : null}
            onClose={() => setDeleteOpen(false)}
            onDeleted={() => navigate({ to: "/artifacts" })}
          />
        </>
      )}
    </div>
  )
}
