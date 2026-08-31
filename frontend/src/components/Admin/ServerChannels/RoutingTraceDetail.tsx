import { useQuery } from "@tanstack/react-query"

import { AdminRoutingService, type RoutingDecisionPublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import { RoutingDiagnosisPanel } from "./RoutingDiagnosisPanel"
import { RoutingRecommendationPanel } from "./RoutingRecommendationPanel"
import { RoutingReplayPanel } from "./RoutingReplayPanel"
import {
  RoutingError,
  RoutingLoading,
  RoutingNotice,
} from "./RoutingStateBlocks"
import { RoutingStagesView } from "./RoutingStagesView"
import {
  formatConfidence,
  formatDateTime,
  formatLatency,
  matchMethodLabel,
  MESSAGE_ABSENT,
  MESSAGE_WITHHELD_NO_NOTICE,
  messageTextState,
  originMeta,
  outcomeMeta,
} from "./routingCopy"
import { parseStages, type RoutingStageCandidate } from "./routingStages"

/** Every candidate across every stage, de-duplicated by `ref_id` — the same
 *  rule the backend's own `stages` reader applies, and for the same reason: a
 *  candidate can legitimately appear in more than one pass. */
function traceCandidates(trace: RoutingDecisionPublic): RoutingStageCandidate[] {
  const byRef = new Map<string, RoutingStageCandidate>()
  for (const stage of parseStages(trace.stages)) {
    for (const candidate of stage.candidates ?? []) {
      if (candidate.ref_id) byRef.set(candidate.ref_id, candidate)
    }
  }
  return [...byRef.values()]
}

/**
 * The full picture for one routing decision: verdict, near-misses, every stage,
 * and the two advisory tools.
 *
 * Takes an already-loaded trace so the same view serves a table row, a simulate
 * result and a re-run — the backend returns the identical type from all three
 * (`RoutingTraceService.get`'s output, verbatim), so the UI does not need three
 * renderers either. Loading and error for the stored-trace case live in
 * `RoutingTraceDetailLoader` below.
 */
export function RoutingTraceDetail({
  trace,
  showActions = true,
}: {
  trace: RoutingDecisionPublic
  /** Off for a fresh simulate result, where "re-run this" has no meaning yet. */
  showActions?: boolean
}) {
  const outcome = outcomeMeta(trace.outcome)
  const origin = originMeta(trace.origin)
  const candidates = traceCandidates(trace)
  const messageState = messageTextState(trace)
  const defaultRefId =
    trace.selected_agent_id ?? trace.selected_bundle_uuid ?? null

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={outcome.tone}>{outcome.label}</Badge>
        <Badge variant={origin.tone}>{origin.label}</Badge>
        <span className="text-xs text-muted-foreground">
          {formatDateTime(trace.created_at)}
        </span>
        {trace.channel_name && (
          <span className="text-xs text-muted-foreground">
            {trace.channel_name}
          </span>
        )}
        {trace.user_email && (
          <span className="text-xs font-medium">{trace.user_email}</span>
        )}
        <span className="text-xs text-muted-foreground">
          {matchMethodLabel(trace.match_method)} ·{" "}
          {formatConfidence(trace.confidence)} confidence ·{" "}
          {formatLatency(trace.latency_ms)}
        </span>
        <code className="font-mono text-[10px] text-muted-foreground">
          {trace.id}
        </code>
      </div>

      {trace.error && (
        <div
          role="alert"
          className="rounded-lg border border-destructive/50 bg-destructive/5 px-3 py-2"
        >
          <p className="text-xs font-medium text-destructive">
            Routing recorded an error on this decision
          </p>
          <p className="mt-0.5 text-xs break-words text-muted-foreground">
            {trace.error}
          </p>
        </div>
      )}

      <div className="space-y-1">
        <p className="text-xs font-medium text-muted-foreground">Message</p>
        {/* Three cases, told apart by `message_sha256` rather than by the
            notice — see `messageTextState`. Branching on the notice would
            claim the gate hid a message for a decision that never had one. */}
        {messageState === "present" ? (
          <p className="rounded bg-muted/50 px-2 py-1.5 text-xs whitespace-pre-wrap break-words">
            {trace.message_text}
          </p>
        ) : messageState === "withheld" ? (
          trace.message_text_notice ? (
            // Server-authored: it states exactly what the gate did and does not
            // do, and names what actually erases stored text. Rendered verbatim
            // so the UI cannot overstate it.
            <RoutingNotice notice={trace.message_text_notice} />
          ) : (
            <RoutingNotice notice={MESSAGE_WITHHELD_NO_NOTICE} />
          )
        ) : (
          <p className="text-xs text-muted-foreground">{MESSAGE_ABSENT}</p>
        )}
      </div>

      <RoutingDiagnosisPanel diagnosis={trace.diagnosis} />

      <RoutingStagesView trace={trace} />

      {showActions && (
        <div className="space-y-3 border-t pt-3">
          <div>
            <p className="mb-1.5 text-xs font-medium text-muted-foreground">
              Re-run
            </p>
            <RoutingReplayPanel traceId={trace.id} />
          </div>
          <div>
            <p className="mb-1.5 text-xs font-medium text-muted-foreground">
              Recommendation for the agent's owner
            </p>
            <RoutingRecommendationPanel
              traceId={trace.id}
              candidates={candidates}
              defaultRefId={defaultRefId}
            />
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * Fetches one stored trace on demand — the expanded-row case.
 *
 * All four states are handled here rather than in `RoutingTraceDetail`: a
 * failed detail fetch must not render as a trace with nothing in it, which
 * would read as "routing considered nobody".
 */
export function RoutingTraceDetailLoader({ traceId }: { traceId: string }) {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["routingTrace", traceId],
    queryFn: () => AdminRoutingService.getRoutingTrace({ traceId }),
  })

  if (isLoading) return <RoutingLoading rows={4} />
  if (isError) {
    return (
      <RoutingError
        error={error}
        fallback="Couldn't load this decision's detail."
        onRetry={() => refetch()}
      />
    )
  }
  if (!data) {
    // A resolved query with no body. Distinct from both the error above and an
    // empty trace: nothing is known here, so nothing is claimed.
    return (
      <p className="text-xs text-muted-foreground">
        The server returned no detail for this decision.
      </p>
    )
  }
  return <RoutingTraceDetail trace={data} />
}
