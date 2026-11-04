import { Link } from "@tanstack/react-router"
import {
  ChevronDown,
  ChevronUp,
  Clock,
  Highlighter,
  Info,
  Loader2,
  MessageSquare,
  RefreshCw,
} from "lucide-react"
import type { ReactNode } from "react"

import type { DashboardTileDataPublic } from "@/client"
import { RowFlag } from "@/components/Common/ListRow"
import {
  formatRelativeTimestamp,
  RelativeTime,
} from "@/components/Common/RelativeTime"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { TileHighlight } from "./highlightStore"

const SKIPPED: Record<string, string> = {
  env_asleep:
    "Not refreshed: the agent's environment is asleep. Opening the tile again wakes it.",
}

function IconButton({
  label,
  onClick,
  pending = false,
  children,
}: {
  label: string
  onClick: () => void
  pending?: boolean
  children: ReactNode
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="h-6 w-6 shrink-0"
          aria-label={label}
          aria-disabled={pending || undefined}
          onClick={pending ? undefined : onClick}
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        {label}
      </TooltipContent>
    </Tooltip>
  )
}

/** "Needs the Gmail credential" / "…Gmail and Slack credentials" / "Needs 3 credentials". */
export function CredentialsSentence({
  agentId,
  names,
}: {
  agentId: string
  names: string[]
}) {
  const noun = names.length === 1 ? "credential" : "credentials"
  const link = (
    <Link
      to="/agent/$agentId"
      params={{ agentId }}
      hash="credentials"
      className="underline-offset-2 hover:underline"
    >
      {noun}
    </Link>
  )
  if (names.length >= 3) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="truncate text-warning">
            Needs {names.length} {link}
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" className="max-w-xs text-xs">
          {names.join(", ")}
        </TooltipContent>
      </Tooltip>
    )
  }
  const who = names.length === 2 ? `${names[0]} and ${names[1]}` : names[0]
  return (
    <span className="truncate text-warning">
      Needs the {who} {link}
    </span>
  )
}

interface Props {
  agentId: string
  agentName: string
  data: DashboardTileDataPublic | undefined
  isHtml: boolean
  isRefreshing: boolean
  runIssue: string | null
  highlight: TileHighlight
  onStep: (delta: 1 | -1) => void
  onRefresh: () => void
  /** Absent on fullscreen and in preview: there is no panel to open. */
  onAsk?: () => void
  onFocusTile?: () => void
}

/**
 * The tile footer (UI spec S1 §4): data age and at most one stale flag, mark
 * and notice on the left; the highlight stepper (native) or flag (html),
 * Refresh and Ask on the right.
 */
export function TileFooter({
  agentId,
  agentName,
  data,
  isHtml,
  isRefreshing,
  runIssue,
  highlight,
  onStep,
  onRefresh,
  onAsk,
  onFocusTile,
}: Props) {
  const missing = data?.missing_credentials ?? []
  const staleReason =
    runIssue ||
    (data?.skipped ? SKIPPED[data.skipped] || data.skipped : null) ||
    (data?.stale ? "Showing the last result while it refreshes" : null)
  const count = highlight.ids.length

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: a click on the footer focuses the tile for the dashboard chat; its buttons stay the keyboard path.
    // biome-ignore lint/a11y/useKeyWithClickEvents: as above — focus is a pointer convenience, Ask is the keyboard route.
    <div
      className="flex h-7 shrink-0 items-center gap-1.5 border-t px-2 text-xs text-muted-foreground"
      onClick={onFocusTile}
    >
      <div className="flex min-w-0 flex-1 items-center gap-1.5">
        {missing.length > 0 ? (
          <CredentialsSentence agentId={agentId} names={missing} />
        ) : (
          <>
            {data?.fetched_at && (
              <span className="truncate">
                Updated <RelativeTime timestamp={data.fetched_at} showTooltip />
              </span>
            )}
            {staleReason && (
              <RowFlag icon={Clock} tone="warning" label={staleReason} />
            )}
            {data?.mark && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Badge
                    variant="outline"
                    className="h-5 min-w-0 max-w-[12rem] shrink truncate"
                  >
                    {data.mark}
                  </Badge>
                </TooltipTrigger>
                <TooltipContent side="top" className="max-w-xs text-xs">
                  {data.mark}
                  {data.mark_at &&
                    ` · ${formatRelativeTimestamp(data.mark_at) ?? ""}`}
                </TooltipContent>
              </Tooltip>
            )}
            {data?.notice && <RowFlag icon={Info} label={data.notice} />}
          </>
        )}
      </div>

      {count > 0 && !isHtml && (
        <div className="flex shrink-0 items-center">
          <IconButton label="Previous highlight" onClick={() => onStep(-1)}>
            <ChevronUp className="h-3.5 w-3.5" />
          </IconButton>
          <span className="tabular-nums">
            {Math.max(highlight.current, 0) + 1}/{count}
          </span>
          <IconButton label="Next highlight" onClick={() => onStep(1)}>
            <ChevronDown className="h-3.5 w-3.5" />
          </IconButton>
        </div>
      )}
      {count > 0 && isHtml && (
        <RowFlag
          icon={Highlighter}
          label={`${count} highlighted by ${agentName}`}
        />
      )}

      {/* One button in both states, so keyboard focus survives the wait:
          the glyph and the tooltip name it, and a click meanwhile is ignored. */}
      <IconButton
        label={isRefreshing ? "Refreshing…" : "Refresh"}
        pending={isRefreshing}
        onClick={onRefresh}
      >
        {isRefreshing ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <RefreshCw className="h-3.5 w-3.5" />
        )}
      </IconButton>
      {onAsk && (
        <IconButton label={`Ask ${agentName}`} onClick={onAsk}>
          <MessageSquare className="h-3.5 w-3.5" />
        </IconButton>
      )}
    </div>
  )
}
