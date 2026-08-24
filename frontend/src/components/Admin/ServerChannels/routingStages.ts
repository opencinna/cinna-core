/**
 * Runtime narrowing for `RoutingDecisionPublic.stages`.
 *
 * **Why this file exists at all.** Everything else on this card uses the
 * generated types from `@/client` directly. `stages` is the one field the
 * generator cannot type: the backend column is JSONB, so the OpenAPI schema
 * says `Array<unknown>` and nothing more. These interfaces are therefore a
 * *narrowing of an untyped payload*, not a hand-written copy of a type the
 * generator already emits.
 *
 * **Every field is optional, and that is deliberate.** Two independent reasons:
 *
 * - `SAFE_STAGE_FIELDS` (`routing_trace.py`) is an allowlist. With
 *   `ROUTING_TRACE_STORE_MESSAGE_TEXT` off, `prompt`, `raw_response` and
 *   `llm_attempts[].error` are simply *absent* from the payload rather than
 *   null. `message_text_notice` explains that to the reader; the components
 *   render the notice, never a guess.
 * - These rows are written by a recorder whose dataclasses keep changing, and
 *   a row written by an older build must still render. The backend's own
 *   `stages` readers are defensive throughout for the same reason.
 *
 * So parsing here never throws and never asserts: an unreadable stage is
 * dropped, an unreadable field comes back `undefined`, and the components
 * treat "absent" as something to say out loud rather than something to fill in.
 */

export interface RoutingStageCandidate {
  kind?: string
  ref_id?: string
  name?: string
  owner_email?: string | null
  source?: string
  trigger_prompt?: string
  prompt_examples?: string | null
  eligible?: boolean
  skip_reason?: string | null
}

export interface RoutingStageLLMAttempt {
  provider?: string
  model?: string | null
  ok?: boolean
  /** Withheld by the allowlist while the message-text gate is off. */
  error?: string | null
  latency_ms?: number
}

export interface RoutingStage {
  stage?: string
  /** `undefined` when the payload has no `candidates` key at all — which is not
   *  the same as a pass that considered nobody. See `parseList`. */
  candidates?: RoutingStageCandidate[]
  match_method?: string | null
  matched_pattern?: string | null
  /** Withheld while the message-text gate is off — it carries the sender's words. */
  prompt?: string | null
  /** Withheld while the message-text gate is off. */
  raw_response?: string | null
  /** `undefined` when the payload has no `llm_attempts` key at all — which is
   *  not the same as a pass that called no provider. See `parseList`. */
  llm_attempts?: RoutingStageLLMAttempt[]
  confidence?: number | null
  reason?: string | null
  runner_up_id?: string | null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined
}

function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined
}

function asBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined
}

function parseCandidate(raw: unknown): RoutingStageCandidate | null {
  if (!isRecord(raw)) return null
  return {
    kind: asString(raw.kind),
    ref_id: asString(raw.ref_id),
    name: asString(raw.name),
    owner_email: asString(raw.owner_email) ?? null,
    source: asString(raw.source),
    trigger_prompt: asString(raw.trigger_prompt),
    prompt_examples: asString(raw.prompt_examples) ?? null,
    eligible: asBoolean(raw.eligible),
    skip_reason: asString(raw.skip_reason) ?? null,
  }
}

function parseAttempt(raw: unknown): RoutingStageLLMAttempt | null {
  if (!isRecord(raw)) return null
  return {
    provider: asString(raw.provider),
    model: asString(raw.model) ?? null,
    ok: asBoolean(raw.ok),
    error: asString(raw.error) ?? null,
    latency_ms: asNumber(raw.latency_ms),
  }
}

/**
 * A list field that is **absent** stays `undefined`; one that is present and
 * empty becomes `[]`.
 *
 * The distinction is load-bearing downstream. "This pass considered no
 * candidates" and "no provider was called in this pass" are *findings about
 * routing*, and they may only be stated from a present-but-empty array. An
 * older or malformed row, where the key is missing entirely, says nothing about
 * what routing did — collapsing the two would report a gap in the instrument as
 * a fact about the system, which is §11a Rule 1 one level down from the
 * `stages == []` case the plan already records.
 */
function parseList<T>(
  raw: unknown,
  parse: (item: unknown) => T | null,
): T[] | undefined {
  if (!Array.isArray(raw)) return undefined
  return raw.map(parse).filter((item): item is T => item !== null)
}

function parseStage(raw: unknown): RoutingStage | null {
  if (!isRecord(raw)) return null
  const candidates = parseList(raw.candidates, parseCandidate)
  const attempts = parseList(raw.llm_attempts, parseAttempt)
  return {
    stage: asString(raw.stage),
    candidates,
    match_method: asString(raw.match_method) ?? null,
    matched_pattern: asString(raw.matched_pattern) ?? null,
    prompt: asString(raw.prompt) ?? null,
    raw_response: asString(raw.raw_response) ?? null,
    llm_attempts: attempts,
    confidence: asNumber(raw.confidence) ?? null,
    reason: asString(raw.reason) ?? null,
    runner_up_id: asString(raw.runner_up_id) ?? null,
  }
}

/** Narrow the generated `Array<unknown>` into something renderable. Never throws. */
export function parseStages(raw: unknown): RoutingStage[] {
  if (!Array.isArray(raw)) return []
  return raw.map(parseStage).filter((s): s is RoutingStage => s !== null)
}

/**
 * Stage labels. A stage name this build has not met renders as itself — the
 * stage vocabulary grows on the backend the same way `origin` does.
 */
const STAGE_LABELS: Record<string, string> = {
  pass_1: "Pass 1 — agents this sender already has",
  pass_2: "Pass 2 — auto-install catalog",
  identity_stage2: "Identity — stage 2",
  app_mcp: "App MCP",
}

export function stageLabel(stage: string | null | undefined): string {
  if (!stage) return "Stage"
  return STAGE_LABELS[stage] ?? stage
}
