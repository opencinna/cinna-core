import { useQuery, useQueryClient } from "@tanstack/react-query"
import {
  CircleCheck,
  CircleDot,
  CircleX,
  List,
  MessageSquareHeart,
  Timer,
} from "lucide-react"
import { useCallback, useState } from "react"

import { ImprovementRequestsService } from "@/client"
import { FilterMenu, type FilterOption } from "@/components/Common/FilterMenu"
import { PREVIEW_COUNT, PreviewList } from "@/components/Common/PreviewList"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { EventTypes, useMultiEventSubscription } from "@/hooks/useEventBus"
import { repeatedSessionIds } from "@/utils/improvementRequests"
import { AllImprovementRequestsSheet } from "./AllImprovementRequestsSheet"
import { ImprovementRequestDetailModal } from "./ImprovementRequestDetailModal"
import { ImprovementRequestRow } from "./ImprovementRequestRow"

const ALL_STATUSES = "all"

type StatusFilter = "all" | "new" | "in_progress" | "completed" | "declined"

const FILTER_OPTIONS: readonly FilterOption<StatusFilter>[] = [
  { value: "all", label: "All", icon: List },
  { value: "new", label: "New", icon: CircleDot },
  { value: "in_progress", label: "In progress", icon: Timer },
  { value: "completed", label: "Completed", icon: CircleCheck },
  { value: "declined", label: "Declined", icon: CircleX },
]

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
  /** The agent serves a REST API, so its callers can file requests too. */
  agentApiEnabled?: boolean
}

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
  agentApiEnabled = false,
}: ImprovementRequestsCardProps) {
  const queryClient = useQueryClient()
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("new")
  const [sheetOpen, setSheetOpen] = useState(false)
  const [selectedRequestId, setSelectedRequestId] = useState<string | null>(
    null,
  )

  const status = statusFilter === ALL_STATUSES ? undefined : statusFilter
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["improvementRequests", agentId, statusFilter, "preview"],
    queryFn: () =>
      ImprovementRequestsService.listAgentImprovementRequests({
        agentId,
        status,
        limit: PREVIEW_COUNT,
      }),
  })

  // Drives the header's "{n} new" badge; `count` is a true total, so one row
  // is enough.
  const { data: newData } = useQuery({
    queryKey: ["improvementRequests", agentId, "new", "count"],
    queryFn: () =>
      ImprovementRequestsService.listAgentImprovementRequests({
        agentId,
        status: "new",
        limit: 1,
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
    // An open detail dialog too (status changed by the building agent).
    queryClient.invalidateQueries({ queryKey: ["improvementRequest"] })
  }, [queryClient])

  useMultiEventSubscription(
    [
      EventTypes.IMPROVEMENT_REQUEST_CREATED,
      EventTypes.IMPROVEMENT_REQUEST_UPDATED,
    ],
    invalidate,
  )

  const requests = data?.data ?? []
  const repeated = repeatedSessionIds(requests)
  const newCount = newData?.count ?? 0
  const filterLabel =
    FILTER_OPTIONS.find((option) => option.value === statusFilter)?.label ??
    "All"

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
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <MessageSquareHeart className="h-5 w-5 shrink-0" />
          Improvement Requests
          {newCount > 0 && (
            <Badge variant="secondary" className="h-5 shrink-0">
              {newCount} new
            </Badge>
          )}
          <div className="ml-auto">
            <FilterMenu
              options={FILTER_OPTIONS}
              value={statusFilter}
              onChange={setStatusFilter}
              allValue={ALL_STATUSES}
            />
          </div>
        </CardTitle>
        <CardDescription>
          Feedback from people who used this agent, and capability requests from
          callers of its API.
        </CardDescription>
      </CardHeader>

      <CardContent>
        <PreviewList
          items={requests}
          total={data?.count ?? 0}
          getKey={(request) => request.id}
          renderItem={(request) => (
            <ImprovementRequestRow
              request={request}
              sharesSession={Boolean(
                request.session_id && repeated.has(request.session_id),
              )}
              onOpen={setSelectedRequestId}
            />
          )}
          isLoading={isLoading}
          isError={isError && !data}
          error={error}
          onRetry={() => refetch()}
          errorFallback="Couldn't load improvement requests"
          empty={
            // The teaching sentence stays in every empty state, not just the
            // unfiltered one: the filter defaults to New, so a brand-new agent
            // would otherwise never show an owner how requests get raised.
            <p className="text-sm text-muted-foreground">
              {statusFilter === ALL_STATUSES
                ? "No improvement requests yet."
                : `No ${filterLabel.toLowerCase()} requests.`}{" "}
              People send one from a session&apos;s ⋮ menu or with{" "}
              <code className="rounded bg-muted px-1 py-0.5 text-xs">
                /session-improve
              </code>
              {agentApiEnabled &&
                ", and callers of this agent's REST API can file capability requests"}
              .
            </p>
          }
          skeletonRows={2}
          skeletonClassName="h-[48px] w-full rounded-md"
          onShowAll={() => setSheetOpen(true)}
        />
      </CardContent>

      <AllImprovementRequestsSheet
        agentId={agentId}
        status={status}
        filterLabel={filterLabel}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
        onOpenRequest={setSelectedRequestId}
      />

      <ImprovementRequestDetailModal
        agentId={agentId}
        requestId={selectedRequestId}
        onClose={() => setSelectedRequestId(null)}
      />
    </Card>
  )
}

export default ImprovementRequestsCard
