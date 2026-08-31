import { useMutation, useQueryClient } from "@tanstack/react-query"
import { ArrowRight, RotateCcw } from "lucide-react"
import { useEffect, useState } from "react"

import { AdminRoutingService, type RoutingReplayDiff } from "@/client"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  RoutingEmpty,
  RoutingLoading,
  RoutingMutationError,
} from "./RoutingStateBlocks"
import { formatConfidence } from "./routingCopy"

/**
 * Re-run a stored decision against current state, and show what moved.
 *
 * The tuning loop's second half: change a trigger prompt, replay the decision
 * that went wrong, see whether it goes right now. The **unchanged** result is a
 * real answer — it says the change did not work — so `diff.summary`, which is
 * server-authored and says "nothing changed" out loud, is always rendered. An
 * empty panel would read as though the replay had not run.
 *
 * The replay is refused (409) when the original's message text is not available
 * to re-run. That arrives as an ordinary API error carrying the server's own
 * explanation, which names the way through (type the message into "Try a
 * message"), so it is rendered rather than replaced.
 */
export function RoutingReplayPanel({ traceId }: { traceId: string }) {
  const queryClient = useQueryClient()
  const [includeCatalog, setIncludeCatalog] = useState(true)

  const mutation = useMutation({
    mutationFn: () =>
      AdminRoutingService.replayRoutingTrace({
        traceId,
        requestBody: { include_catalog: includeCatalog },
      }),
    onSuccess: () => {
      // A replay is stored as its own `origin="simulate"` trace, so the list
      // behind this panel is now stale.
      queryClient.invalidateQueries({ queryKey: ["routingTraces"] })
    },
  })

  // A diff computed under the other catalog setting, still on screen after the
  // checkbox moves, would be a result attributed to inputs that are no longer
  // shown. Drop it instead.
  const { reset } = mutation
  useEffect(() => {
    reset()
  }, [reset, includeCatalog])

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        <LoadingButton
          size="sm"
          variant="outline"
          className="h-8 text-xs"
          loading={mutation.isPending}
          onClick={() => mutation.mutate()}
        >
          <RotateCcw className="mr-1.5 h-3 w-3" />
          Re-run against current state
        </LoadingButton>
        <div className="flex items-center gap-2">
          <Checkbox
            id={`replay-catalog-${traceId}`}
            checked={includeCatalog}
            onCheckedChange={(checked) => setIncludeCatalog(checked === true)}
          />
          <Label
            htmlFor={`replay-catalog-${traceId}`}
            className="text-xs font-normal text-muted-foreground"
          >
            Include the auto-install catalog
          </Label>
        </div>
      </div>

      {mutation.isPending ? (
        <RoutingLoading rows={2} />
      ) : mutation.isError ? (
        <RoutingMutationError
          error={mutation.error}
          fallback="Couldn't re-run this decision."
          onRetry={() => mutation.mutate()}
        />
      ) : mutation.data ? (
        <ReplayDiffView diff={mutation.data.diff} />
      ) : (
        <RoutingEmpty
          title="Not re-run yet."
          hint="Re-running spends a real LLM call and writes nothing to this trace."
        />
      )}
    </div>
  )
}

function DiffRow({
  label,
  before,
  after,
}: {
  label: string
  before: string
  after: string
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="w-28 shrink-0 text-muted-foreground">{label}</span>
      <span className="font-mono">{before}</span>
      <ArrowRight className="h-3 w-3 text-muted-foreground" />
      <span className="font-mono font-medium">{after}</span>
    </div>
  )
}

function ReplayDiffView({ diff }: { diff: RoutingReplayDiff }) {
  const rows: { label: string; before: string; after: string }[] = []
  if (diff.outcome_changed) {
    rows.push({
      label: "Outcome",
      before: diff.original_outcome ?? "—",
      after: diff.replay_outcome ?? "—",
    })
  }
  if (diff.selection_changed) {
    rows.push({
      label: "Selection",
      before: diff.original_selection ?? "nothing",
      after: diff.replay_selection ?? "nothing",
    })
  }
  if (diff.match_method_changed) {
    rows.push({
      label: "Match method",
      before: diff.original_match_method ?? "—",
      after: diff.replay_match_method ?? "—",
    })
  }
  if (diff.original_confidence !== diff.replay_confidence) {
    rows.push({
      label: "Confidence",
      before: formatConfidence(diff.original_confidence),
      after: formatConfidence(diff.replay_confidence),
    })
  }
  if (diff.original_candidate_count !== diff.replay_candidate_count) {
    rows.push({
      label: "Candidates",
      before: String(diff.original_candidate_count ?? 0),
      after: String(diff.replay_candidate_count ?? 0),
    })
  }

  const added = diff.candidates_added ?? []
  const removed = diff.candidates_removed ?? []

  return (
    <div className="space-y-2 rounded-lg border p-3">
      {/* Server-authored, verbatim — including the "nothing changed" wording,
          which is an answer and not an empty state. Rendered unconditionally:
          gating it on truthiness would quietly break the promise this panel's
          docstring makes if the string were ever empty. */}
      <p className="text-sm font-medium break-words">
        {diff.summary || "The server returned no summary for this re-run."}
      </p>

      {rows.length > 0 ? (
        <div className="space-y-1">
          {rows.map((row) => (
            <DiffRow key={row.label} {...row} />
          ))}
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">
          No field-level differences between the original and the re-run.
        </p>
      )}

      {added.length > 0 && (
        <p className="text-xs break-words">
          <span className="text-muted-foreground">Candidates added: </span>
          {added.join(", ")}
        </p>
      )}
      {removed.length > 0 && (
        <p className="text-xs break-words">
          <span className="text-muted-foreground">Candidates removed: </span>
          {removed.join(", ")}
        </p>
      )}
    </div>
  )
}
