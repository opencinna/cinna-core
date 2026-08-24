import { Check, ChevronDown, ChevronRight } from "lucide-react"
import { useState } from "react"

import type { RoutingDecisionPublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { RoutingEmpty } from "./RoutingStateBlocks"
import {
  formatConfidence,
  formatLatency,
  matchMethodLabel,
  skipReasonLabel,
  sourceLabel,
} from "./routingCopy"
import {
  parseStages,
  type RoutingStage,
  type RoutingStageCandidate,
  stageLabel,
} from "./routingStages"

/**
 * The expanded row's mechanical half: what each routing pass actually saw.
 *
 * Reads `stages`, which the generator types as `Array<unknown>` (JSONB) — see
 * `routingStages.ts` for why the narrowing lives in this feature and why every
 * field is optional.
 *
 * The gated fields (`prompt`, `raw_response`, `llm_attempts[].error`) are
 * *absent*, not empty, while `ROUTING_TRACE_STORE_MESSAGE_TEXT` is off. This
 * component never renders "the model returned nothing" for a field that was
 * withheld — the trace-level `message_text_notice` says which case it is, and
 * `RoutingTraceDetail` renders that notice above this view.
 */

function chosenRefId(trace: RoutingDecisionPublic): string | null {
  return trace.selected_agent_id ?? trace.selected_bundle_uuid ?? null
}

function CandidateTable({
  candidates,
  chosen,
}: {
  candidates: RoutingStageCandidate[] | undefined
  chosen: string | null
}) {
  if (candidates === undefined) {
    // No `candidates` key on this stage at all. That is a gap in the record,
    // not an observation about routing, and it must not be stated as one.
    return (
      <p className="px-1 text-xs text-muted-foreground">
        This trace records no candidate list for this pass.
      </p>
    )
  }
  if (candidates.length === 0) {
    // Present and empty: a pass that ran and considered nobody. *That* is a
    // finding — the stage exists precisely so "this pass ran" is observable —
    // so it gets a sentence rather than an omitted section.
    return (
      <p className="px-1 text-xs text-muted-foreground">
        This pass considered no candidates.
      </p>
    )
  }

  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="text-xs">Agent</TableHead>
            <TableHead className="text-xs">Owner</TableHead>
            <TableHead className="text-xs">Source</TableHead>
            <TableHead className="text-xs">Trigger prompt</TableHead>
            <TableHead className="text-xs">Eligible</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {candidates.map((candidate, index) => {
            const isChosen =
              chosen !== null && !!candidate.ref_id && candidate.ref_id === chosen
            return (
              <TableRow
                key={candidate.ref_id || `candidate-${index}`}
                className={isChosen ? "bg-primary/5" : undefined}
              >
                <TableCell className="text-xs">
                  <div className="flex items-center gap-1.5">
                    {isChosen && (
                      <Check
                        className="h-3.5 w-3.5 shrink-0 text-primary"
                        aria-label="Chosen"
                      />
                    )}
                    <span className="font-medium">
                      {candidate.name || candidate.ref_id || "(unnamed)"}
                    </span>
                    {/* Unknown kinds render as themselves. */}
                    {candidate.kind && (
                      <Badge variant="outline" className="text-[10px]">
                        {candidate.kind}
                      </Badge>
                    )}
                  </div>
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {candidate.owner_email || "—"}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {sourceLabel(candidate.source)}
                </TableCell>
                <TableCell className="max-w-[22rem] text-xs">
                  {candidate.trigger_prompt ? (
                    <span className="break-words whitespace-pre-wrap">
                      {candidate.trigger_prompt}
                    </span>
                  ) : (
                    <span className="text-muted-foreground">— none —</span>
                  )}
                  {candidate.prompt_examples && (
                    <span className="mt-1 block break-words whitespace-pre-wrap text-muted-foreground">
                      {candidate.prompt_examples}
                    </span>
                  )}
                </TableCell>
                <TableCell className="text-xs">
                  {candidate.eligible === false ? (
                    <Badge variant="outline" className="text-[10px]">
                      {skipReasonLabel(candidate.skip_reason)}
                    </Badge>
                  ) : candidate.eligible === true ? (
                    <span className="text-muted-foreground">Yes</span>
                  ) : (
                    // Neither true nor false: the field is missing from an
                    // older payload. Not the same as "not eligible".
                    <span className="text-muted-foreground">unknown</span>
                  )}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

function AttemptsTable({
  stage,
  textGated,
}: {
  stage: RoutingStage
  textGated: boolean
}) {
  const attempts = stage.llm_attempts
  if (attempts === undefined) {
    // No `llm_attempts` key at all — an older or malformed row. Says nothing
    // about whether a provider was reached.
    return (
      <p className="px-1 text-xs text-muted-foreground">
        This trace records no provider attempts for this pass.
      </p>
    )
  }
  if (attempts.length === 0) {
    // Present and empty: a pattern match or a single-candidate shortcut never
    // reaches a provider. Saying so beats an empty section that reads as "the
    // providers failed".
    return (
      <p className="px-1 text-xs text-muted-foreground">
        No provider was called in this pass.
      </p>
    )
  }
  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="text-xs">Provider</TableHead>
            <TableHead className="text-xs">Model</TableHead>
            <TableHead className="text-xs">Result</TableHead>
            <TableHead className="text-xs">Latency</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {attempts.map((attempt, index) => (
            <TableRow key={`${attempt.provider ?? "provider"}-${index}`}>
              <TableCell className="font-mono text-xs">
                {attempt.provider || "—"}
              </TableCell>
              <TableCell className="font-mono text-xs text-muted-foreground">
                {attempt.model || "—"}
              </TableCell>
              <TableCell className="text-xs">
                {attempt.ok === true ? (
                  <Badge variant="secondary" className="text-[10px]">
                    ok
                  </Badge>
                ) : attempt.ok === false ? (
                  <div className="space-y-0.5">
                    <Badge variant="destructive" className="text-[10px]">
                      failed
                    </Badge>
                    {attempt.error ? (
                      <p className="break-words text-[11px] text-muted-foreground">
                        {attempt.error}
                      </p>
                    ) : (
                      // `error` is deliberately NOT in `SAFE_LLM_ATTEMPT_FIELDS`
                      // (`routing_trace.py`), so with the message-text gate
                      // closed a failed attempt *always* arrives without a
                      // reason. A bare "failed" would read as "the recorder
                      // captured nothing" — and this is precisely the case the
                      // card advertises answering ("is my local LLM broken?"),
                      // where the provider error is the answer.
                      <p className="break-words text-[11px] text-muted-foreground">
                        {textGated
                          ? "Reason withheld — see the notice above."
                          : "No reason recorded."}
                      </p>
                    )}
                  </div>
                ) : (
                  <span className="text-muted-foreground">unknown</span>
                )}
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {formatLatency(attempt.latency_ms)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

function Disclosure({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <Button
        variant="ghost"
        size="sm"
        className="h-7 px-1 text-xs"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="mr-1 h-3 w-3" />
        ) : (
          <ChevronRight className="mr-1 h-3 w-3" />
        )}
        {label}
      </Button>
      {open && <div className="mt-1">{children}</div>}
    </div>
  )
}

function TextBlock({ text }: { text: string }) {
  return (
    <pre className="max-h-72 overflow-auto rounded bg-muted/50 px-2 py-1.5 text-[11px] whitespace-pre-wrap break-words">
      {text}
    </pre>
  )
}

function StageBlock({
  stage,
  chosen,
  textGated,
}: {
  stage: RoutingStage
  chosen: string | null
  /** The message-text gate withheld the sender's words from this payload. */
  textGated: boolean
}) {
  return (
    <div className="space-y-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">{stageLabel(stage.stage)}</span>
        {stage.match_method && (
          <Badge variant="secondary" className="text-[10px]">
            {matchMethodLabel(stage.match_method)}
          </Badge>
        )}
        {stage.matched_pattern && (
          <code className="font-mono text-[11px] text-muted-foreground">
            {stage.matched_pattern}
          </code>
        )}
        {stage.confidence !== null && stage.confidence !== undefined && (
          <span className="text-xs text-muted-foreground">
            confidence {formatConfidence(stage.confidence)}
          </span>
        )}
      </div>

      {stage.reason && (
        <p className="text-xs break-words text-muted-foreground">
          {stage.reason}
        </p>
      )}

      <CandidateTable candidates={stage.candidates} chosen={chosen} />
      <AttemptsTable stage={stage} textGated={textGated} />

      <div className="space-y-1">
        {stage.raw_response ? (
          <Disclosure label="Raw LLM response">
            <TextBlock text={stage.raw_response} />
          </Disclosure>
        ) : (
          <p className="px-1 text-xs text-muted-foreground">
            {textGated
              ? "Raw LLM response withheld — see the notice above."
              : "No raw LLM response on this pass."}
          </p>
        )}
        {stage.prompt ? (
          <Disclosure label="Rendered classifier prompt">
            <TextBlock text={stage.prompt} />
          </Disclosure>
        ) : (
          <p className="px-1 text-xs text-muted-foreground">
            {textGated
              ? "Rendered prompt withheld — see the notice above."
              : "No rendered prompt on this pass."}
          </p>
        )}
      </div>
    </div>
  )
}

export function RoutingStagesView({ trace }: { trace: RoutingDecisionPublic }) {
  const stages = parseStages(trace.stages)
  const chosen = chosenRefId(trace)
  const textGated = trace.message_text_hidden === true

  if (stages.length === 0) {
    // The recorder creates a stage eagerly on capture entry so that "this pass
    // ran" is observable by construction (plan §11a Rule 1). An empty list is
    // therefore a gap in the instrument, and it is labelled as one — it is not
    // evidence that routing did nothing.
    return (
      <RoutingEmpty
        title="This decision recorded no stages."
        hint="The trace loaded but carries no stage detail — an older row, or a capture that ended before a pass began. It is not a statement that routing skipped every pass."
      />
    )
  }

  return (
    <div className="space-y-2">
      {stages.map((stage, index) => (
        <StageBlock
          key={`${stage.stage ?? "stage"}-${index}`}
          stage={stage}
          chosen={chosen}
          textGated={textGated}
        />
      ))}
    </div>
  )
}
