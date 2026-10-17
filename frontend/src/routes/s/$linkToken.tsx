import { useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useCallback, useState } from "react"

import { ApiError, ArtifactLinksService } from "@/client"
import { artifactTitle } from "@/components/Artifacts/artifactFormat"
import { viewRefreshInterval } from "@/components/Artifacts/artifactQueries"
import {
  PublicLinkBar,
  PublicLinkCodeScreen,
  PublicLinkError,
  PublicLinkLoading,
} from "@/components/Artifacts/PublicLink/PublicLinkScreens"
import { ArtifactViewer } from "@/components/Artifacts/viewers/ArtifactViewer"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/s/$linkToken")({
  component: PublicArtifactLink,
  validateSearch: (search: Record<string, unknown>): { file?: string } => ({
    file: typeof search.file === "string" ? search.file : undefined,
  }),
  head: () => ({ meta: [{ title: APP_NAME }] }),
})

const EXPIRED =
  "This link has expired. Ask the person who shared it for a new one."
const BLOCKED =
  "This link was blocked after too many wrong codes. Ask the person who shared it for a new one."
const GONE = "This link doesn't exist or was turned off."

/** Sources an agent publishes from; an `upload` (or a CLI push) is a person's. */
const AGENT_SOURCES = new Set(["core_agent", "desktop"])

interface CodeRefusal {
  message?: string
  attempts_left?: number
  blocked?: boolean
}

function refusalOf(err: unknown): CodeRefusal | null {
  if (!(err instanceof ApiError) || err.status !== 403) return null
  const detail = (err.body as { detail?: unknown } | undefined)?.detail
  return detail && typeof detail === "object" ? (detail as CodeRefusal) : {}
}

function statusOf(err: unknown): number | null {
  return err instanceof ApiError ? err.status : null
}

/**
 * S9 — a signed-out recipient opens what they were sent: the Cinna bar, the
 * code prompt when the link has one, then the viewer.
 */
function PublicArtifactLink() {
  const { linkToken } = Route.useParams()
  const search = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })
  const queryClient = useQueryClient()
  // Held in memory only, so the cap refresh can re-open without re-prompting.
  const [code, setCode] = useState<string | null>(null)
  const [codeError, setCodeError] = useState<string | null>(null)
  const [blocked, setBlocked] = useState(false)

  const info = useQuery({
    queryKey: ["artifactLinkInfo", linkToken],
    queryFn: () =>
      ArtifactLinksService.getArtifactLinkInfo({ token: linkToken }),
    retry: false,
    refetchOnWindowFocus: false,
  })

  const needsCode = info.data?.needs_code ?? false
  const canOpen =
    info.data !== undefined &&
    !info.data.expired &&
    !info.data.blocked &&
    !blocked &&
    (!needsCode || code !== null)

  const view = useQuery({
    queryKey: ["artifactLinkView", linkToken, code],
    queryFn: () =>
      ArtifactLinksService.openArtifactLink({
        token: linkToken,
        requestBody: { code },
      }),
    enabled: canOpen,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Number.POSITIVE_INFINITY,
    refetchInterval: (query) => viewRefreshInterval(query.state.data),
  })

  const selectFile = useCallback(
    (path: string) => navigate({ search: { file: path } }),
    [navigate],
  )

  const submitCode = async (entered: string) => {
    try {
      const opened = await ArtifactLinksService.openArtifactLink({
        token: linkToken,
        requestBody: { code: entered },
      })
      queryClient.setQueryData(["artifactLinkView", linkToken, entered], opened)
      setCodeError(null)
      setCode(entered)
    } catch (err) {
      const refusal = refusalOf(err)
      if (refusal?.blocked) {
        setBlocked(true)
        return
      }
      if (refusal) {
        const left = refusal.attempts_left
        setCodeError(
          left === undefined
            ? refusal.message || "Incorrect code."
            : `Incorrect code. ${left} ${left === 1 ? "attempt" : "attempts"} left.`,
        )
        return
      }
      const status = statusOf(err)
      setCodeError(
        status === 410 || status === 404
          ? null
          : "Couldn't check the code. Try again.",
      )
      if (status === 410 || status === 404) void info.refetch()
    }
  }

  if (info.isError) {
    return (
      <PublicLinkError
        message={
          statusOf(info.error) === 404
            ? GONE
            : "Couldn't open this link right now. Try again in a moment."
        }
      />
    )
  }
  if (!info.data) return <PublicLinkLoading />
  if (info.data.expired) return <PublicLinkError message={EXPIRED} />
  if (info.data.blocked || blocked) return <PublicLinkError message={BLOCKED} />

  const title = artifactTitle(info.data)
  if (needsCode && code === null) {
    return (
      <PublicLinkCodeScreen
        title={title}
        error={codeError}
        onSubmit={submitCode}
      />
    )
  }

  if (view.isError) {
    const status = statusOf(view.error)
    if (status === 410) return <PublicLinkError message={EXPIRED} />
    if (status === 404) return <PublicLinkError message={GONE} />
    const refusal = refusalOf(view.error)
    if (refusal?.blocked) return <PublicLinkError message={BLOCKED} />
  }

  const owner = info.data.owner_name?.trim()
  const byline = [
    owner ? `Shared by ${owner}` : "",
    AGENT_SOURCES.has(info.data.source_kind) ? "made by an agent" : "",
  ]
    .filter(Boolean)
    .join(" · ")

  return (
    <div className="flex h-dvh flex-col bg-background">
      <PublicLinkBar
        title={title}
        byline={byline}
        downloadHref={view.data?.download_url ?? null}
        description={info.data.description?.trim() || ""}
      />
      <main className="min-h-0 flex-1">
        <ArtifactViewer
          title={title}
          view={view.data}
          isError={view.isError}
          onRetry={() => view.refetch()}
          selectedFile={search.file}
          onSelectFile={selectFile}
        />
      </main>
    </div>
  )
}
