import {
  Download,
  ExternalLink,
  PanelRight,
  Pencil,
  Share2,
  Trash2,
} from "lucide-react"

import type { ArtifactDetailPublic } from "@/client"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { artifactTitle } from "../artifactFormat"
import { SourceText } from "./ArtifactDetailsPanel"

function IconLink({
  href,
  label,
  icon: Icon,
  newTab,
}: {
  href: string | null
  label: string
  icon: typeof Download
  newTab?: boolean
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {href ? (
          <Button variant="ghost" size="icon" className="h-8 w-8" asChild>
            <a
              href={href}
              aria-label={label}
              {...(newTab
                ? { target: "_blank", rel: "noopener noreferrer" }
                : {})}
            >
              <Icon className="h-4 w-4" />
            </a>
          </Button>
        ) : (
          // Still a tooltip target while the view loads; nothing to follow yet.
          <span tabIndex={-1}>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              aria-label={label}
              disabled
            >
              <Icon className="h-4 w-4" />
            </Button>
          </span>
        )}
      </TooltipTrigger>
      <TooltipContent side="bottom" className="text-xs">
        {label}
      </TooltipContent>
    </Tooltip>
  )
}

interface ArtifactPageHeaderProps {
  artifact: ArtifactDetailPublic
  /** The revision on screen when it is not the current one. */
  oldRevision: number | null
  onShowCurrent: () => void
  downloadHref: string | null
  fullScreenHref: string | null
  detailsOpen: boolean
  onToggleDetails: () => void
  onShare: () => void
  onEdit: () => void
  onDelete: () => void
}

/** S4's app-header content: title and subline left, ≤ 4 controls right. */
export function ArtifactPageHeader({
  artifact,
  oldRevision,
  onShowCurrent,
  downloadHref,
  fullScreenHref,
  detailsOpen,
  onToggleDetails,
  onShare,
  onEdit,
  onDelete,
}: ArtifactPageHeaderProps) {
  const title = artifactTitle(artifact)
  const shownRevision = oldRevision ?? artifact.current_revision_number

  return (
    <>
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2">
          <h1 className="truncate text-lg font-semibold">{title}</h1>
          {oldRevision !== null && (
            <>
              <Badge variant="outline" className="h-5 shrink-0">
                Revision {oldRevision} · not current
              </Badge>
              <Button
                variant="link"
                size="sm"
                className="h-auto shrink-0 p-0"
                onClick={onShowCurrent}
              >
                Show current
              </Button>
            </>
          )}
        </div>
        <p className="truncate text-xs text-muted-foreground">
          <SourceText artifact={artifact} />
          {shownRevision ? ` · Revision ${shownRevision}` : ""} · updated{" "}
          <RelativeTime timestamp={artifact.updated_at} showTooltip />
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {artifact.is_owner && (
          <Button size="sm" onClick={onShare}>
            <Share2 className="h-4 w-4" />
            Share
          </Button>
        )}
        <IconLink href={downloadHref} label="Download as zip" icon={Download} />
        <IconLink
          href={fullScreenHref}
          label="Open in a new tab"
          icon={ExternalLink}
          newTab
        />
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 aria-pressed:bg-muted"
              aria-pressed={detailsOpen}
              aria-label="Details"
              onClick={onToggleDetails}
            >
              <PanelRight className="h-4 w-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom" className="text-xs">
            {detailsOpen ? "Hide details" : "Show details"}
          </TooltipContent>
        </Tooltip>
        {artifact.is_owner && (
          <RowActionsMenu label={`the artifact ${title}`}>
            <DropdownMenuItem onSelect={onEdit}>
              <Pencil />
              Edit details…
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onSelect={onDelete}>
              <Trash2 />
              Delete artifact
            </DropdownMenuItem>
          </RowActionsMenu>
        )}
      </div>
    </>
  )
}
