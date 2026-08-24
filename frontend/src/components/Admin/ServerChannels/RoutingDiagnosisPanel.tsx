import { Lightbulb, Target } from "lucide-react"

import type { RoutingDiagnosisPublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import { RoutingNotice } from "./RoutingStateBlocks"
import {
  diagnosisTone,
  formatSimilarity,
  skipReasonLabel,
} from "./routingCopy"

/**
 * The card's headline: why this decision went the way it did.
 *
 * **Every user-facing sentence in here comes from the API.** `verdict`,
 * `action` and `near_miss_notice` are computed by
 * `routing_reachability_service` and pinned by backend tests — twenty-two
 * verdict sentences' worth. Nothing in this component composes, paraphrases or
 * maps a `code` to its own copy: if the card started authoring this text the
 * backend tests would stop covering what an admin actually reads, and the
 * wording would drift away from the rules it describes. `code` is used for one
 * thing only, and it is a colour.
 */

/**
 * `verdict` is *composed from* `action` (the model's own docstring), so
 * rendering both verbatim would print the remedy twice. Split at the boundary
 * the API guarantees instead — this moves server-authored characters around, it
 * never rewrites them, and any input that does not satisfy the invariant falls
 * back to rendering both fields in full.
 */
function splitVerdict(verdict: string, action: string) {
  const trimmedAction = action.trim()
  if (!trimmedAction) return { lead: verdict, action: "" }
  if (verdict === trimmedAction) return { lead: verdict, action: "" }
  if (verdict.endsWith(trimmedAction)) {
    const lead = verdict.slice(0, verdict.length - trimmedAction.length).trim()
    // A verdict that is nothing but its remedy: keep the whole sentence as the
    // lead rather than emptying the headline.
    if (!lead) return { lead: verdict, action: "" }
    return { lead, action: trimmedAction }
  }
  return { lead: verdict, action: trimmedAction }
}

const TONE_CLASSES: Record<string, string> = {
  ok: "border-primary/40 bg-primary/5",
  warn: "border-amber-500/50 bg-amber-500/5",
  bad: "border-destructive/50 bg-destructive/5",
  neutral: "border-border bg-muted/40",
}

export function RoutingDiagnosisPanel({
  diagnosis,
}: {
  diagnosis: RoutingDiagnosisPublic | null | undefined
}) {
  // Loading and error for this data belong to the trace query that carries it
  // (`diagnosis` is a field of `RoutingDecisionPublic`, delivered in the same
  // response). What is left here is the third state: the request succeeded and
  // the server returned no diagnosis. Said plainly, so it cannot be read as a
  // failure — or as a verdict.
  if (!diagnosis) {
    // Documented as "None only when the diagnosis itself failed" — `diagnose()`
    // is total and returns a `unavailable`-coded verdict instead, so this is
    // defensive. It still gets the failure register rather than the dashed
    // "empty" one: if it is ever reached, something broke, and an empty-looking
    // box would read as "there was nothing to say".
    return (
      <div
        role="alert"
        className="rounded-lg border border-destructive/50 bg-destructive/5 px-3 py-2"
      >
        <p className="text-sm font-medium text-destructive">
          No diagnosis for this decision.
        </p>
        <p className="mt-0.5 text-xs text-muted-foreground">
          The trace loaded but carried no verdict — the diagnosis failed to
          compute. It is not a finding about routing.
        </p>
      </div>
    )
  }

  const tone = diagnosisTone(diagnosis.code)
  const { lead, action } = splitVerdict(diagnosis.verdict, diagnosis.action)
  const skipped = Object.entries(diagnosis.skipped_by_reason ?? {})
  const nearMisses = diagnosis.near_misses ?? []

  return (
    <div className={`space-y-3 rounded-lg border p-3 ${TONE_CLASSES[tone]}`}>
      <div className="flex items-start gap-2">
        <Target className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1 space-y-2">
          {/* Server-authored, verbatim. */}
          <p className="text-sm font-medium break-words">{lead}</p>
          {action && (
            <p className="flex items-start gap-1.5 text-sm break-words">
              <Lightbulb className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              {/* Server-authored, verbatim. */}
              <span>{action}</span>
            </p>
          )}
          <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
            <Badge variant="outline" className="font-mono text-[10px]">
              {diagnosis.code}
            </Badge>
            <span className="text-xs text-muted-foreground">
              {diagnosis.eligible_candidate_count ?? 0} eligible candidate
              {(diagnosis.eligible_candidate_count ?? 0) === 1 ? "" : "s"}
            </span>
            {skipped.map(([reason, count]) => (
              <Badge key={reason} variant="outline" className="text-[10px]">
                {skipReasonLabel(reason)} ×{count}
              </Badge>
            ))}
          </div>
          {diagnosis.expected_agent_name && (
            <p className="text-xs text-muted-foreground">
              Asked about{" "}
              <span className="font-medium">
                {diagnosis.expected_agent_name}
              </span>
              {diagnosis.expected_agent_owner_email
                ? ` (${diagnosis.expected_agent_owner_email})`
                : ""}
            </p>
          )}
        </div>
      </div>

      <NearMisses
        nearMisses={nearMisses}
        notice={diagnosis.near_miss_notice ?? null}
      />
    </div>
  )
}

/**
 * The near-miss ranking, and the one case a naive `.map()` gets wrong.
 *
 * The backend distinguishes "nothing came close" (a finding) from "we could not
 * rank" (the message text was gated off, or no candidate had a trigger prompt).
 * It expresses the second as an **empty list plus a `near_miss_notice`** — and
 * `_rank_near_misses` returns a notice on *every* empty branch. Mapping the
 * list and rendering nothing when it is empty would discard that distinction
 * and silently turn "we could not measure" into "nothing scored", which is the
 * exact class of lie this feature exists to remove.
 *
 * So the notice is checked first and rendered verbatim, and the case where both
 * are absent gets its own line rather than borrowing either meaning.
 */
function NearMisses({
  nearMisses,
  notice,
}: {
  nearMisses: NonNullable<RoutingDiagnosisPublic["near_misses"]>
  notice: string | null
}) {
  // The notice is rendered *alongside* the ranking, never instead of it. Today
  // the backend only ever pairs a notice with an empty list, but that is an
  // invariant this component does not own — a future "ranking is partial
  // because X" notice must not silently delete the ranking it qualifies.
  if (notice) {
    return (
      <div className="space-y-1">
        <p className="text-xs font-medium text-muted-foreground">
          Closest candidates
        </p>
        <RoutingNotice notice={notice} />
        {nearMisses.length > 0 && <NearMissList nearMisses={nearMisses} />}
      </div>
    )
  }

  if (nearMisses.length === 0) {
    // Neither a ranking nor a reason. The server always pairs an empty ranking
    // with a notice, so reaching this means the payload is older or shorter
    // than expected — which is a fact about the trace, not about the agents.
    return (
      <p className="text-xs text-muted-foreground">
        No near-miss ranking in this trace, and no reason given for its absence.
      </p>
    )
  }

  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">
        Closest candidates
      </p>
      <NearMissList nearMisses={nearMisses} />
    </div>
  )
}

function NearMissList({
  nearMisses,
}: {
  nearMisses: NonNullable<RoutingDiagnosisPublic["near_misses"]>
}) {
  return (
    <div className="space-y-1">
      <ul className="space-y-1">
        {nearMisses.map((miss) => (
          <li
            key={`${miss.kind}:${miss.ref_id}`}
            className="flex flex-wrap items-center gap-2 text-xs"
          >
            <span className="font-mono text-muted-foreground">
              {formatSimilarity(miss.similarity)}
            </span>
            <span className="font-medium">{miss.name}</span>
            {/* An unknown `kind` renders as itself rather than disappearing. */}
            <Badge variant="outline" className="text-[10px]">
              {miss.kind || "unknown"}
            </Badge>
            {miss.eligible === false && (
              <Badge variant="outline" className="text-[10px]">
                {skipReasonLabel(miss.skip_reason)}
              </Badge>
            )}
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">
        Token overlap with the routed message — a hint, not a threshold. Routing
        applies no similarity cut-off.
      </p>
    </div>
  )
}
