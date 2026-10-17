import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { AlertTriangle, Search, Upload, X } from "lucide-react"
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react"

import type { ArtifactPublic } from "@/client"
import { DataTable } from "@/components/Common/DataTable"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import PendingArtifacts from "@/components/Pending/PendingArtifacts"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useWorkspace from "@/hooks/useWorkspace"
import { APP_NAME } from "@/utils"
import { ArtifactSegments } from "./ArtifactSegments"
import { getArtifactColumns } from "./artifactColumns"
import {
  type ArtifactScope,
  artifactKeys,
  deleteArtifactMutationKey,
  readAllArtifacts,
} from "./artifactQueries"
import { DeleteArtifactDialog } from "./DeleteArtifactDialog"
import { ArtifactShareDialog } from "./Share/ArtifactShareDialog"
import { UploadArtifactDialog } from "./UploadArtifactDialog"

const SCOPE_TO_HASH: Record<ArtifactScope, string> = {
  mine: "mine",
  shared_with_me: "shared",
}
const HASH_TO_SCOPE: Record<string, ArtifactScope> = {
  mine: "mine",
  shared: "shared_with_me",
}

function getInitialScope(): ArtifactScope {
  return HASH_TO_SCOPE[window.location.hash.slice(1)] ?? "mine"
}

function matchesQuery(artifact: ArtifactPublic, query: string): boolean {
  return [artifact.title, artifact.source_label, artifact.owner_name]
    .filter(Boolean)
    .some((field) => (field as string).toLowerCase().includes(query))
}

function EmptySection({
  text,
  children,
}: {
  text: string
  children?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center gap-3 py-6 whitespace-normal">
      <p className="max-w-md text-sm text-muted-foreground">{text}</p>
      {children}
    </div>
  )
}

/** S2 body: the scope control, the search, and one `DataTable` per scope. */
export function ArtifactsList() {
  const { workspaceFilter } = useWorkspace()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [scope, setScope] = useState<ArtifactScope>(getInitialScope)
  const [search, setSearch] = useState("")
  const searchRef = useRef<HTMLInputElement>(null)
  const [deleteTarget, setDeleteTarget] = useState<ArtifactPublic | null>(null)
  const [shareTarget, setShareTarget] = useState<ArtifactPublic | null>(null)

  // Both clear controls unmount on click, so focus moves to the input.
  const clearSearch = () => {
    setSearch("")
    searchRef.current?.focus()
  }

  const handleScopeChange = (next: ArtifactScope) => {
    setScope(next)
    window.location.hash = SCOPE_TO_HASH[next]
  }

  useEffect(() => {
    const handleHashChange = () => {
      const next = HASH_TO_SCOPE[window.location.hash.slice(1)]
      if (next) setScope(next)
    }
    window.addEventListener("hashchange", handleHashChange)
    return () => window.removeEventListener("hashchange", handleHashChange)
  }, [])

  // Mine is workspace-filtered; Shared is not (the credentials semantics).
  const mine = useQuery({
    queryKey: artifactKeys.list("mine", workspaceFilter),
    queryFn: () => readAllArtifacts("mine", workspaceFilter),
  })
  const shared = useQuery({
    queryKey: artifactKeys.list("shared_with_me"),
    queryFn: () => readAllArtifacts("shared_with_me", undefined),
  })

  const query = search.trim().toLowerCase()
  const scopeRows = scope === "mine" ? mine.data : shared.data
  const visibleRows = useMemo(
    () => (scopeRows ?? []).filter((a) => !query || matchesQuery(a, query)),
    [scopeRows, query],
  )

  const columns = useMemo(
    () =>
      getArtifactColumns({
        onShare: setShareTarget,
        onDelete: setDeleteTarget,
      }),
    [],
  )

  if (mine.isError && mine.data === undefined) {
    return (
      <QueryErrorAlert
        error={mine.error}
        fallback="Couldn't load your artifacts."
        onRetry={() => mine.refetch()}
      />
    )
  }

  const sharedLoading = shared.data === undefined && !shared.isError
  const loading = scope === "mine" ? mine.data === undefined : sharedLoading

  const emptyState = query ? (
    <EmptySection text={`No artifacts match "${search.trim()}".`}>
      <Button variant="link" size="sm" onClick={clearSearch}>
        Clear search
      </Button>
    </EmptySection>
  ) : scope === "mine" ? (
    <EmptySection text="Artifacts your agents publish, and files you upload, appear here.">
      <UploadArtifactDialog
        trigger={
          <Button size="sm">
            <Upload className="h-4 w-4" />
            Upload
          </Button>
        }
      />
    </EmptySection>
  ) : shared.isError ? (
    <EmptySection text="Nothing to show here until the artifacts shared with you load." />
  ) : (
    <EmptySection
      text={`Artifacts people share with you, or with everyone on ${APP_NAME}, appear here.`}
    />
  )

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <ArtifactSegments
          label="Artifact scope"
          value={scope}
          onChange={handleScopeChange}
          segments={[
            {
              value: "mine",
              label: "Mine",
              count: mine.data === undefined ? null : mine.data.length,
            },
            {
              value: "shared_with_me",
              label: "Shared with me",
              count: shared.isError
                ? undefined
                : shared.data === undefined
                  ? null
                  : shared.data.length,
            },
          ]}
        />
        <div className="relative min-w-40 max-w-64 flex-1">
          <Search className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            ref={searchRef}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search"
            aria-label="Search artifacts by title, source or owner"
            className="pl-9 pr-9"
          />
          {search && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="absolute top-1/2 right-1 h-7 w-7 -translate-y-1/2"
                  onClick={clearSearch}
                  aria-label="Clear search"
                >
                  <X className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                Clear search
              </TooltipContent>
            </Tooltip>
          )}
        </div>
      </div>

      {shared.isError && (
        <Alert className="border-warning/50 text-warning">
          <AlertTriangle />
          <AlertTitle>
            Artifacts shared with you could not be loaded.
          </AlertTitle>
          <AlertDescription className="flex flex-wrap items-center gap-2">
            <span>Shared with me may be missing rows.</span>
            <Button
              variant="outline"
              size="sm"
              className="h-7"
              onClick={() => shared.refetch()}
            >
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {loading ? (
        <PendingArtifacts />
      ) : (
        <DataTable
          // A new scope or search starts at page 1; a refetch keeps the page.
          key={`${scope}|${query}`}
          autoResetPageIndex={false}
          columns={columns}
          data={visibleRows}
          getRowId={(row) => row.id}
          tableClassName="table-fixed"
          hideHeader
          itemLabel="artifacts"
          emptyState={emptyState}
          onRowClick={(row) => {
            // A row whose delete is in flight is not a way into it.
            if (
              queryClient.isMutating({
                mutationKey: deleteArtifactMutationKey(row.id),
              }) > 0
            ) {
              return
            }
            navigate({
              to: "/artifact/$artifactId",
              params: { artifactId: row.id },
            })
          }}
        />
      )}

      <DeleteArtifactDialog
        artifact={deleteTarget}
        onClose={() => setDeleteTarget(null)}
      />
      <ArtifactShareDialog
        artifact={shareTarget}
        onClose={() => setShareTarget(null)}
      />
    </div>
  )
}
