import { useIsMutating } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import {
  Building2,
  Link as LinkGlyph,
  Loader2,
  Share2,
  Trash2,
  Users,
} from "lucide-react"

import type { ArtifactPublic } from "@/client"
import { RowFlag, RowInfo } from "@/components/Common/ListRow"
import {
  formatRelativeTimestamp,
  RelativeTime,
} from "@/components/Common/RelativeTime"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useCopyToClipboard } from "@/hooks/useCopyToClipboard"
import useCustomToast from "@/hooks/useCustomToast"
import { APP_NAME } from "@/utils"
import {
  artifactKindIcon,
  artifactSource,
  artifactTitle,
  formatBytes,
  plural,
} from "./artifactFormat"
import { deleteArtifactMutationKey } from "./artifactQueries"

/** Widths shared by the table and its loading skeleton. */
export const ARTIFACT_COLUMN_CLASSES = {
  name: "",
  source: "hidden lg:table-cell w-[240px]",
  updated: "w-[120px]",
  actions: "w-12 px-2 text-right",
} as const

/** The sharing flags, owner rows only; a private artifact renders none. */
function SharingFlags({ artifact }: { artifact: ArtifactPublic }) {
  if (!artifact.is_owner) return null
  const grants = artifact.grant_count ?? 0
  const links = artifact.live_link_count ?? 0
  return (
    <>
      {artifact.visibility === "instance" && (
        <RowFlag
          icon={Building2}
          label={`Everyone on ${APP_NAME} can open it`}
        />
      )}
      {grants > 0 && (
        <RowFlag
          icon={Users}
          label={`Shared with ${plural(grants, "person", "people")}`}
        />
      )}
      {links > 0 && (
        <RowFlag
          icon={LinkGlyph}
          label={`${plural(links, "public link")} ${links === 1 ? "works" : "work"}`}
        />
      )}
    </>
  )
}

function NameCell({ artifact }: { artifact: ArtifactPublic }) {
  const Icon = artifactKindIcon(artifact)
  const title = artifactTitle(artifact)
  const revisions = artifact.revision_count ?? 1
  const created = formatRelativeTimestamp(artifact.created_at)

  return (
    <div className="flex min-w-0 items-center gap-2">
      <Icon
        className="h-4 w-4 shrink-0 text-muted-foreground"
        aria-hidden="true"
      />
      <Tooltip>
        <TooltipTrigger asChild>
          {/* The link is the row's accessible target; the row click only
              widens the hit area. */}
          <Link
            to="/artifact/$artifactId"
            params={{ artifactId: artifact.id }}
            className="min-w-0 truncate rounded-sm text-sm font-medium outline-none hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/50"
          >
            {title}
          </Link>
        </TooltipTrigger>
        <TooltipContent side="top" className="max-w-sm break-words text-xs">
          {title}
        </TooltipContent>
      </Tooltip>
      <span className="flex shrink-0 items-center gap-1.5">
        <SharingFlags artifact={artifact} />
        <RowInfo
          facts={[
            artifact.current_revision_number
              ? `Revision ${artifact.current_revision_number} · ${plural(revisions, "revision")}`
              : null,
            `${formatBytes(artifact.size_bytes)} · ${plural(artifact.file_count ?? 0, "file")}`,
            created && `Created ${created}`,
          ]}
        />
      </span>
    </div>
  )
}

function SourceCell({ artifact }: { artifact: ArtifactPublic }) {
  // A shared row is scanned by who shared it, so the owner comes first.
  const text = artifact.is_owner
    ? artifactSource(artifact)
    : [artifact.owner_name?.trim(), artifactSource(artifact)]
        .filter(Boolean)
        .join(" · ")
  return (
    <p className="truncate text-xs text-muted-foreground" title={text}>
      {text}
    </p>
  )
}

function ActionsCell({
  artifact,
  onShare,
  onDelete,
}: {
  artifact: ArtifactPublic
  onShare: (artifact: ArtifactPublic) => void
  onDelete: (artifact: ArtifactPublic) => void
}) {
  const title = artifactTitle(artifact)
  const deleting =
    useIsMutating({ mutationKey: deleteArtifactMutationKey(artifact.id) }) > 0
  const [, copy] = useCopyToClipboard()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  if (deleting) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className="flex h-7 w-7 items-center justify-center"
            aria-live="polite"
          >
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
            <span className="sr-only">Deleting {title}…</span>
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          Deleting…
        </TooltipContent>
      </Tooltip>
    )
  }

  const copyLink = async () => {
    if (await copy(artifact.url)) showSuccessToast("Link copied")
    else showErrorToast("Couldn't copy the link")
  }

  return (
    <RowActionsMenu label={`the artifact ${title}`}>
      <DropdownMenuItem onSelect={() => void copyLink()}>
        <LinkGlyph />
        Copy link
      </DropdownMenuItem>
      {artifact.is_owner && (
        <>
          <DropdownMenuItem onSelect={() => onShare(artifact)}>
            <Share2 />
            Share…
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            variant="destructive"
            onSelect={() => onDelete(artifact)}
          >
            <Trash2 />
            Delete artifact
          </DropdownMenuItem>
        </>
      )}
    </RowActionsMenu>
  )
}

export function getArtifactColumns({
  onShare,
  onDelete,
}: {
  onShare: (artifact: ArtifactPublic) => void
  onDelete: (artifact: ArtifactPublic) => void
}): ColumnDef<ArtifactPublic>[] {
  return [
    {
      id: "name",
      header: "Name",
      cell: ({ row }) => <NameCell artifact={row.original} />,
    },
    {
      id: "source",
      header: "Source",
      meta: { className: ARTIFACT_COLUMN_CLASSES.source },
      cell: ({ row }) => <SourceCell artifact={row.original} />,
    },
    {
      id: "updated",
      header: "Updated",
      meta: { className: ARTIFACT_COLUMN_CLASSES.updated },
      cell: ({ row }) => (
        <RelativeTime
          timestamp={row.original.updated_at}
          showTooltip
          className="text-xs text-muted-foreground whitespace-nowrap"
        />
      ),
    },
    {
      id: "actions",
      header: () => <span className="sr-only">Actions</span>,
      meta: { className: ARTIFACT_COLUMN_CLASSES.actions },
      cell: ({ row }) => (
        <div className="flex justify-end">
          <ActionsCell
            artifact={row.original}
            onShare={onShare}
            onDelete={onDelete}
          />
        </div>
      ),
    },
  ]
}
