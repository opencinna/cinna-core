import { Link } from "@tanstack/react-router"
import type { ReactNode } from "react"

import type { ArtifactDetailPublic } from "@/client"
import { PreviewList } from "@/components/Common/PreviewList"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { APP_NAME } from "@/utils"
import {
  artifactKindWord,
  artifactSource,
  artifactTitle,
  formatBytes,
  plural,
} from "../artifactFormat"
import { RevisionRow } from "./RevisionRow"

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm break-words">{children}</dd>
    </div>
  )
}

/** "Only you · 3 people · 2 public links" — the Access fact's value. */
function accessSummary(artifact: ArtifactDetailPublic): string {
  const parts = [
    artifact.visibility === "instance" ? `Everyone on ${APP_NAME}` : "Only you",
  ]
  const grants = artifact.grant_count ?? 0
  const links = artifact.live_link_count ?? 0
  if (grants > 0) parts.push(plural(grants, "person", "people"))
  if (links > 0) parts.push(plural(links, "public link"))
  return parts.join(" · ")
}

/**
 * The source. For the owner of an artifact published from a session
 * (`source_session_id` is owner-only on the wire), a plain link to that
 * session (§2 Links: no glyph); otherwise text.
 */
export function SourceText({ artifact }: { artifact: ArtifactDetailPublic }) {
  const label = artifactSource(artifact)
  if (!artifact.is_owner || !artifact.source_session_id) return <>{label}</>
  return (
    <Link
      to="/session/$sessionId"
      params={{ sessionId: artifact.source_session_id }}
      search={{
        initialMessage: undefined,
        fileIds: undefined,
        fileObjects: undefined,
        pageContext: undefined,
      }}
      className="underline-offset-4 hover:underline"
    >
      {label}
    </Link>
  )
}

export function ArtifactDetailsPanelSkeleton() {
  return (
    <div className="space-y-3 p-4">
      <Skeleton className="h-4 w-24" />
      {Array.from({ length: 4 }).map((_, i) => (
        <Skeleton key={i} className="h-8 w-full" />
      ))}
    </div>
  )
}

interface ArtifactDetailsPanelProps {
  artifact: ArtifactDetailPublic
  /** The revision on screen (`null` while the current one is). */
  viewedNumber: number | null
  onViewRevision: (number: number) => void
  onEdit: () => void
  onShare: () => void
  /**
   * Opens the revisions Sheet. The route owns it: below `xl` this panel is
   * itself a Sheet, which must close first (§2 Disclosure depth).
   */
  onShowAllRevisions: () => void
}

/**
 * The artifact page's details: About (description and facts) and, for the
 * owner, Revisions — the P7 skeleton of sections split by `Separator`s.
 */
export function ArtifactDetailsPanel({
  artifact,
  viewedNumber,
  onViewRevision,
  onEdit,
  onShare,
  onShowAllRevisions,
}: ArtifactDetailsPanelProps) {
  const title = artifactTitle(artifact)
  const revisions = [...(artifact.revisions ?? [])].sort(
    (a, b) => b.number - a.number,
  )
  const shownNumber = viewedNumber ?? artifact.current_revision_number ?? null

  return (
    <div className="space-y-4 p-4">
      <section className="space-y-3">
        <h3 className="text-sm font-medium">About</h3>
        {artifact.description ? (
          <p className="text-sm break-words whitespace-pre-wrap">
            {artifact.description}
          </p>
        ) : artifact.is_owner ? (
          <p className="text-sm text-muted-foreground">
            No description.{" "}
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0"
              onClick={onEdit}
            >
              Add one
            </Button>
          </p>
        ) : null}
        <dl className="space-y-2">
          {!artifact.is_owner && (
            <Fact label="Owner">
              {artifact.owner_name?.trim() || "Unknown person"}
            </Fact>
          )}
          <Fact label="Source">
            <SourceText artifact={artifact} />
          </Fact>
          <Fact label="Kind">{artifactKindWord(artifact.kind)}</Fact>
          <Fact label="Size">{formatBytes(artifact.size_bytes)}</Fact>
          <Fact label="Files">{plural(artifact.file_count ?? 0, "file")}</Fact>
          <Fact label="Updated">
            <RelativeTime timestamp={artifact.updated_at} showTooltip />
          </Fact>
          <Fact label="Created">
            <RelativeTime timestamp={artifact.created_at} showTooltip />
          </Fact>
          {artifact.is_owner && (
            <Fact label="Access">
              <Button
                variant="link"
                size="sm"
                className="h-auto p-0 text-left whitespace-normal"
                onClick={onShare}
              >
                {accessSummary(artifact)}
              </Button>
            </Fact>
          )}
        </dl>
      </section>

      {artifact.is_owner && (
        <>
          <Separator />
          <section className="space-y-2">
            <h3 className="text-sm font-medium">Revisions</h3>
            <PreviewList
              items={revisions}
              getKey={(r) => r.id}
              errorFallback="Couldn't load the revisions."
              empty={
                <p className="text-sm text-muted-foreground">No revisions.</p>
              }
              renderItem={(revision) => (
                <RevisionRow
                  artifactId={artifact.id}
                  artifactTitle={title}
                  revision={revision}
                  isViewed={revision.number === shownNumber}
                  onView={onViewRevision}
                />
              )}
              onShowAll={onShowAllRevisions}
            />
          </section>
        </>
      )}
    </div>
  )
}
