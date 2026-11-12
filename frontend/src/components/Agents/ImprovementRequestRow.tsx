/**
 * One improvement request, as a P3 row that is itself the Details control
 * (§2 Row as the control). Shared by the card and "Show all" Sheet so the two
 * cannot drift.
 *
 * Two kinds read differently on purpose: an API capability request is about
 * *what* was asked (its title), a session request is about *who* sent which
 * conversation. The kind is a `RowFlag`, never a second badge; the one
 * title-line `Badge` is the status, a workflow word the owner scans for.
 */
import { Braces, Layers } from "lucide-react"

import type { ImprovementRequestPublic } from "@/client"
import { ListRow, RowFlag, RowInfo } from "@/components/Common/ListRow"
import { formatRelativeTimestamp } from "@/components/Common/RelativeTime"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import {
  getImprovementStatusMeta,
  isApiCapabilityRequest,
} from "@/utils/improvementRequests"

interface ImprovementRequestRowProps {
  request: ImprovementRequestPublic
  /** Another visible request captured the same session. */
  sharesSession: boolean
  onOpen: (requestId: string) => void
}

export function ImprovementRequestRow({
  request,
  sharesSession,
  onOpen,
}: ImprovementRequestRowProps) {
  const status = getImprovementStatusMeta(request.status)
  const requester = request.requester_display || "Unknown requester"
  const submitted = formatRelativeTimestamp(request.created_at)
  const isApi = isApiCapabilityRequest(request)
  const title = isApi ? request.title || "Untitled API request" : requester
  const meta = isApi
    ? [requester, request.source_agent_name].filter(Boolean).join(" · ")
    : undefined

  // `role="button"` makes the label the row's whole accessible name, so the
  // flags' `sr-only` text is restated here.
  const ariaLabel = isApi
    ? `Open API capability request "${title}" from ${requester}, ${status.label}${submitted ? `, ${submitted}` : ""}`
    : `Open improvement request from ${requester}, ${status.label}${submitted ? `, ${submitted}` : ""}${sharesSession ? ", same session as another request" : ""}`

  const open = (e: React.SyntheticEvent) => {
    // Clicks bubbling through a portal (a tooltip) are not the row's.
    if (!e.currentTarget.contains(e.target as Node)) return
    onOpen(request.id)
  }

  return (
    // biome-ignore lint/a11y/useSemanticElements: `ListRow` renders `div`s, which a real `<button>` cannot contain — the same trade the addon rows make.
    <div
      role="button"
      tabIndex={0}
      aria-label={ariaLabel}
      className="cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
      onClick={open}
      onKeyDown={(e) => {
        if (e.key !== "Enter" && e.key !== " ") return
        if (e.target !== e.currentTarget) return
        e.preventDefault()
        onOpen(request.id)
      }}
    >
      <ListRow
        title={title}
        badges={
          <Badge
            variant={status.variant}
            className={cn("h-5 shrink-0", status.className)}
          >
            {status.label}
          </Badge>
        }
        meta={meta || undefined}
        flags={
          <>
            {isApi && <RowFlag icon={Braces} label="API capability request" />}
            {!isApi && sharesSession && (
              <RowFlag
                icon={Layers}
                label="Same session as another request in this list"
              />
            )}
            <RowInfo facts={[submitted && `Submitted ${submitted}`]} />
          </>
        }
      />
    </div>
  )
}
