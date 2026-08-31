import { useMutation } from "@tanstack/react-query"
import { Check, Copy, Wand2 } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import {
  AdminRoutingService,
  type RoutingRecommendationPublic,
} from "@/client"
import { Button } from "@/components/ui/button"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import useCustomToast from "@/hooks/useCustomToast"
import {
  RoutingEmpty,
  RoutingLoading,
  RoutingMutationError,
  RoutingNotice,
} from "./RoutingStateBlocks"
import { READ_ONLY_BOUNDARY } from "./routingCopy"
import type { RoutingStageCandidate } from "./routingStages"

/**
 * Draft a trigger prompt for one candidate — **a recommendation for that
 * agent's owner, and nothing else.**
 *
 * Plan §1 makes this a hard boundary: the admin surface is read-only with
 * respect to agents. It never edits another user's agent, trigger prompt or
 * bundle. There is deliberately **no** "apply" control anywhere in this panel,
 * not even for an agent the acting admin happens to own — a button that looked
 * like it applied the draft would misdescribe the endpoint, which writes
 * nothing at all.
 *
 * The boundary sentence shown to the user is the server's own `notice`
 * (`RECOMMENDATION_ADVISORY_NOTICE`), rendered verbatim, so the promise the UI
 * makes and the promise the route keeps are the same string.
 */
export function RoutingRecommendationPanel({
  traceId,
  candidates,
  defaultRefId,
}: {
  traceId: string
  candidates: RoutingStageCandidate[]
  defaultRefId: string | null
}) {
  const { showErrorToast } = useCustomToast()
  const pickable = candidates.filter((c) => !!c.ref_id)
  // A `defaultRefId` that is not among the candidates — an older row with no
  // stage detail — would leave the trigger blank instead of showing its
  // placeholder.
  const initialRefId =
    defaultRefId && pickable.some((c) => c.ref_id === defaultRefId)
      ? defaultRefId
      : ""
  const [refId, setRefId] = useState<string>(initialRefId)

  const mutation = useMutation({
    mutationFn: (): Promise<RoutingRecommendationPublic> =>
      AdminRoutingService.draftRoutingRecommendation({
        traceId,
        requestBody: { ref_id: refId || null },
      }),
  })

  // With no candidate to name and no obvious subject on the trace, the request
  // can only come back as an error — so the button does not offer the trip.
  const canDraft = refId !== "" || pickable.length > 0

  // A draft describes the candidate it was asked about; switching candidates
  // must not leave the previous one's wording on screen under a new name.
  const { reset } = mutation
  useEffect(() => {
    reset()
  }, [reset, refId])

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Select value={refId} onValueChange={setRefId}>
          <SelectTrigger className="h-8 w-[16rem] text-xs">
            <SelectValue
              placeholder={
                pickable.length === 0
                  ? "No candidate on this trace"
                  : "Draft for which candidate?"
              }
            />
          </SelectTrigger>
          <SelectContent>
            {pickable.map((candidate) => (
              <SelectItem
                key={candidate.ref_id as string}
                value={candidate.ref_id as string}
              >
                {candidate.name || candidate.ref_id}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <LoadingButton
          size="sm"
          variant="outline"
          className="h-8 text-xs"
          loading={mutation.isPending}
          disabled={!canDraft}
          onClick={() => mutation.mutate()}
        >
          <Wand2 className="mr-1.5 h-3 w-3" />
          Draft a recommendation
        </LoadingButton>
      </div>

      <p className="text-xs text-muted-foreground">{READ_ONLY_BOUNDARY}</p>

      {mutation.isPending ? (
        <RoutingLoading rows={2} />
      ) : mutation.isError ? (
        <RoutingMutationError
          error={mutation.error}
          fallback="Couldn't draft a recommendation."
          onRetry={() => mutation.mutate()}
        />
      ) : mutation.data ? (
        <RecommendationResult
          result={mutation.data}
          onCopyError={() => showErrorToast("Failed to copy the draft")}
        />
      ) : (
        <RoutingEmpty
          title="No draft yet."
          hint="Pick a candidate and draft wording you can send to its owner."
        />
      )}
    </div>
  )
}

function RecommendationResult({
  result,
  onCopyError,
}: {
  result: RoutingRecommendationPublic
  onCopyError: () => void
}) {
  // The generator's own failure comes back as a 200 with `success: false` — it
  // is itself a diagnosis ("is my local LLM broken?"), so it renders as a
  // stated failure rather than as an empty draft.
  if (result.success === false) {
    return (
      <div
        role="alert"
        className="rounded-lg border border-destructive/50 bg-destructive/5 px-3 py-2"
      >
        <p className="text-sm font-medium text-destructive">
          The draft generator failed.
        </p>
        <p className="mt-1 text-xs break-words text-muted-foreground">
          {result.error || "The provider gave no reason."}
        </p>
      </div>
    )
  }

  if (!result.suggested_trigger_prompt) {
    return (
      <RoutingEmpty
        title="The generator returned no wording."
        hint="It reported success but produced an empty draft — nothing to send on."
      />
    )
  }

  return (
    <div className="space-y-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium">
          Suggested trigger prompt for {result.name}
          {result.owner_email ? ` · owner ${result.owner_email}` : ""}
        </p>
        <CopyButton
          value={result.suggested_trigger_prompt}
          onError={onCopyError}
        />
      </div>

      <pre className="max-h-60 overflow-auto rounded bg-muted/50 px-2 py-1.5 text-xs whitespace-pre-wrap break-words">
        {result.suggested_trigger_prompt}
      </pre>

      {result.current_trigger_prompt && (
        <div>
          <p className="text-xs font-medium text-muted-foreground">
            Current wording
          </p>
          <pre className="max-h-40 overflow-auto rounded bg-muted/30 px-2 py-1.5 text-xs whitespace-pre-wrap break-words">
            {result.current_trigger_prompt}
          </pre>
        </div>
      )}

      {/* Server-authored boundary statement, verbatim. */}
      {result.notice && <RoutingNotice notice={result.notice} />}
    </div>
  )
}

function CopyButton({
  value,
  onError,
}: {
  value: string
  onError: () => void
}) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )

  const copy = async () => {
    // `navigator.clipboard` is undefined outside a secure context and
    // `writeText` rejects when permission is denied. Unhandled, both leave a
    // button that visibly does nothing.
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      timer.current = setTimeout(() => setCopied(false), 2000)
    } catch {
      onError()
    }
  }

  return (
    <Button
      variant="outline"
      size="sm"
      className="h-7 text-xs"
      onClick={copy}
    >
      {copied ? (
        <Check className="mr-1.5 h-3 w-3" />
      ) : (
        <Copy className="mr-1.5 h-3 w-3" />
      )}
      {copied ? "Copied" : "Copy"}
    </Button>
  )
}
