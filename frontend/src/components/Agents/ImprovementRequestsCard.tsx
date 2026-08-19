import { useQuery, useQueryClient } from "@tanstack/react-query"
import { MessageSquareHeart } from "lucide-react"
import { useCallback, useState } from "react"

import { ImprovementRequestsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { EventTypes, useMultiEventSubscription } from "@/hooks/useEventBus"
import { getErrorMessage } from "@/utils"
import {
  getImprovementStatusLabel,
  getImprovementStatusMeta,
  IMPROVEMENT_STATUSES,
} from "@/utils/improvementRequests"
import { ImprovementRequestDetailModal } from "./ImprovementRequestDetailModal"

const ALL_STATUSES = "all"

interface ImprovementRequestsCardProps {
  agentId: string
  /**
   * Render nothing when the agent has received no requests at all.
   *
   * Used on foreign installs. Requests raised against a bundle install
   * normally route away to the publisher, so the card would sit empty — but
   * `resolve_target` falls back to self when the publisher install is gone,
   * and those requests land here and nowhere else. Hiding only the empty case
   * keeps the tab uncluttered without stranding them.
   */
  hideWhenEmpty?: boolean
}

const formatDate = (value: string) =>
  new Date(value).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  })

/**
 * Improvement requests received on an agent the current user owns.
 *
 * Owner-only surface: `AgentConfigTab` skips it entirely on foreign installs.
 * It is deliberately *not* gated on the developer role — receiving feedback on
 * an agent you own is an owner capability, not a developer one.
 */
export function ImprovementRequestsCard({
  agentId,
  hideWhenEmpty = false,
}: ImprovementRequestsCardProps) {
  const queryClient = useQueryClient()
  const [statusFilter, setStatusFilter] = useState<string>("new")
  const [selectedRequestId, setSelectedRequestId] = useState<string | null>(
    null,
  )

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["improvementRequests", agentId, statusFilter],
    queryFn: () =>
      ImprovementRequestsService.listAgentImprovementRequests({
        agentId,
        status: statusFilter === ALL_STATUSES ? undefined : statusFilter,
      }),
  })

  // Drives the header badge and the "New" filter option's count. When the
  // filter already *is* "new" this shares a cache entry with the query above,
  // so it costs nothing in the default state.
  const { data: newData } = useQuery({
    queryKey: ["improvementRequests", agentId, "new"],
    queryFn: () =>
      ImprovementRequestsService.listAgentImprovementRequests({
        agentId,
        status: "new",
      }),
  })

  // Unfiltered existence probe for `hideWhenEmpty`. The list query above is
  // filtered (New by default), so it cannot answer "does this agent have any
  // at all". `limit: 1` keeps it to one row; `count` is a true total.
  const { data: anyData, isPending: anyPending } = useQuery({
    queryKey: ["improvementRequests", agentId, "any"],
    queryFn: () =>
      ImprovementRequestsService.listAgentImprovementRequests({
        agentId,
        limit: 1,
      }),
    enabled: hideWhenEmpty,
  })

  // Live invalidation. Without the socket the card still refetches on mount and
  // window focus like any other query, so a dropped connection only costs
  // freshness.
  // Deliberately the id-free prefix: `useMultiEventSubscription` captures the
  // handler once at subscribe time and never refreshes it, so a handler closing
  // over `agentId` would keep invalidating the previous agent's key if this
  // component ever survived an id change.
  const invalidate = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["improvementRequests"] })
  }, [queryClient])

  useMultiEventSubscription(
    [
      EventTypes.IMPROVEMENT_REQUEST_CREATED,
      EventTypes.IMPROVEMENT_REQUEST_UPDATED,
    ],
    invalidate,
  )

  const requests = data?.data ?? []

  // Session ids that appear on more than one visible row. Computed over the
  // page actually rendered — the marker means "and another one you can see
  // here", which is the only claim this page can support.
  const repeatedSessionIds = new Set(
    requests
      .map((r) => r.session_id)
      .filter(
        (id, _i, all): id is string =>
          Boolean(id) && all.filter((other) => other === id).length > 1,
      ),
  )
  const newCount = newData?.count ?? 0

  // Stay hidden while the probe is in flight, so the card does not flash in
  // and out on every foreign install.
  if (hideWhenEmpty && (anyPending || (anyData?.count ?? 0) === 0)) {
    return null
  }

  // `GET /agents/{id}` admits superusers, but `list_for_agent` authorises
  // strictly on ownership and answers 404 — deliberately, so an id the caller
  // is not party to is indistinguishable from one that does not exist. That is
  // "this card is not for you", not a failure worth showing, so it renders
  // nothing rather than an error next to cards that loaded fine.
  if ((error as { status?: number } | null)?.status === 404) {
    return null
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="space-y-1.5 min-w-0">
            <CardTitle className="flex items-center gap-2">
              <MessageSquareHeart className="h-5 w-5" />
              Improvement Requests
              {newCount > 0 && (
                <Badge className="bg-violet-100 text-violet-700 dark:bg-violet-950/50 dark:text-violet-300">
                  {newCount} new
                </Badge>
              )}
            </CardTitle>
            <CardDescription>
              Feedback from people who used this agent, with the session that
              triggered it.
            </CardDescription>
          </div>
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger className="w-[150px] shrink-0" size="sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_STATUSES}>All</SelectItem>
              {IMPROVEMENT_STATUSES.map((status) => (
                <SelectItem key={status} value={status}>
                  {getImprovementStatusLabel(status)}
                  {status === "new" && newCount > 0 ? ` (${newCount})` : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </CardHeader>

      <CardContent>
        {isError ? (
          <p className="text-sm text-destructive">
            {getErrorMessage(error, "Couldn't load improvement requests.")}
          </p>
        ) : isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : requests.length === 0 ? (
          // The teaching sentence stays in every empty state, not just the
          // unfiltered one: the filter defaults to New, so a brand-new agent
          // would otherwise never show an owner how requests get raised.
          <p className="text-sm text-muted-foreground">
            {statusFilter === ALL_STATUSES
              ? "No improvement requests yet."
              : `No ${getImprovementStatusLabel(
                  statusFilter,
                ).toLowerCase()} requests.`}{" "}
            Users can send one from a session&apos;s ⋮ menu or with{" "}
            <code className="text-xs bg-muted px-1 py-0.5 rounded">
              /session-improve
            </code>
            .
          </p>
        ) : (
          // A compact list, not a data table: who / when / status is three
          // fields, and <table> markup for that is more chrome than content.
          // Each row is a real <button>, so keyboard activation, the focus
          // ring and the accessible name all come from the element itself —
          // no role override, no hand-rolled Enter/Space handler, and none of
          // the table-semantics trade-off that came with `role="button"` on a
          // <tr>. Everything else lives in the detail modal.
          <div className="-mx-2 space-y-0.5">
            {requests.map((request) => {
              const statusMeta = getImprovementStatusMeta(request.status)
              const requester = request.requester_display || "Unknown requester"
              // Two captures of the same conversation are one report, not two —
              // usually a re-submit after the first went unanswered. Saying so
              // here saves opening both to find out.
              const sharesSession = Boolean(
                request.session_id && repeatedSessionIds.has(request.session_id),
              )
              return (
                <button
                  key={request.id}
                  type="button"
                  onClick={() => setSelectedRequestId(request.id)}
                  aria-label={`Open improvement request from ${requester}, ${
                    statusMeta.label
                  }, ${formatDate(request.created_at)}${
                    sharesSession ? ", same session as another request" : ""
                  }`}
                  className="w-full flex items-center gap-3 rounded-md px-2 py-1.5 text-left text-sm transition-colors hover:bg-muted/60 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
                >
                  <span className="min-w-0 flex-1 truncate">{requester}</span>
                  {sharesSession && (
                    <span className="shrink-0 whitespace-nowrap text-xs text-muted-foreground">
                      same session
                    </span>
                  )}
                  <span className="shrink-0 whitespace-nowrap text-xs text-muted-foreground">
                    {formatDate(request.created_at)}
                  </span>
                  <Badge
                    className={`shrink-0 text-xs ${statusMeta.badgeClass}`}
                  >
                    {statusMeta.label}
                  </Badge>
                </button>
              )
            })}
          </div>
        )}
        {/* The list takes the backend's default page size; say so rather than
            silently showing a prefix as if it were everything. */}
        {(data?.count ?? 0) > requests.length && (
          <p className="mt-2 text-xs text-muted-foreground">
            Showing {requests.length} of {data?.count}. Use{" "}
            <code className="bg-muted px-1 py-0.5 rounded">
              cinna improve list
            </code>{" "}
            to see them all.
          </p>
        )}
      </CardContent>

      <ImprovementRequestDetailModal
        agentId={agentId}
        requestId={selectedRequestId}
        onClose={() => setSelectedRequestId(null)}
      />
    </Card>
  )
}

export default ImprovementRequestsCard
