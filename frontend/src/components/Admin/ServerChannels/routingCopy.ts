/**
 * Shared vocabulary for the Auto Routing Tuning card.
 *
 * Two rules govern everything in this file:
 *
 * 1. **No diagnosis prose lives here.** `diagnosis.verdict`, `diagnosis.action`,
 *    `message_text_notice`, `near_miss_notice`, `diff.summary` and the
 *    recommendation `notice` are computed and tested on the backend
 *    (plan §9/§10) and are rendered verbatim by the components. What this file
 *    holds is *labels* for machine codes and chrome copy for controls — the
 *    same split `ChannelDebugDialog`'s `KIND_META` already makes.
 * 2. **An unknown code must still render.** Every lookup below falls through to
 *    the raw value rather than to a blank. The `origin` and `outcome`
 *    vocabularies grow on the backend without a migration (see the route
 *    docstrings), and `origin="simulate"` is itself brand new — a card that
 *    blanks on a value it has not met fails exactly when something new is
 *    happening.
 */

export type BadgeTone = "outline" | "secondary" | "destructive" | "default"

interface Meta {
  label: string
  tone: BadgeTone
}

/** Terminal verdict of a routing pass. `no_match` is the interesting one. */
const OUTCOME_META: Record<string, Meta> = {
  routed: { label: "Routed", tone: "default" },
  no_match: { label: "No match", tone: "outline" },
  error: { label: "Error", tone: "destructive" },
  parked_install: { label: "Parked install", tone: "secondary" },
}

/** Which entry point captured the trace. */
const ORIGIN_META: Record<string, Meta> = {
  server_channel: { label: "Channel", tone: "secondary" },
  app_mcp: { label: "App MCP", tone: "secondary" },
  identity: { label: "Identity", tone: "secondary" },
  simulate: { label: "Simulate", tone: "outline" },
}

/** How the winner was picked, when there was one. */
const MATCH_METHOD_LABELS: Record<string, string> = {
  pattern: "pattern match",
  ai: "AI classifier",
  only_one: "only candidate",
}

/** Why a candidate never reached the classifier. */
const SKIP_REASON_LABELS: Record<string, string> = {
  already_installed: "Already installed",
  bundle_missing: "Bundle missing",
  pass_1_matched: "Pass 1 matched first",
  not_installable: "Not installable",
  no_trigger_prompt: "No trigger prompt",
  identity_route: "Identity route",
  foreign_owner: "Foreign owner",
  route_inactive: "Route inactive",
  no_revision: "No published revision",
  agent_missing: "Agent missing",
}

/** Where a candidate came from. */
const SOURCE_LABELS: Record<string, string> = {
  admin: "Admin",
  user: "User",
  identity: "Identity",
  catalog: "Catalog",
}

export function outcomeMeta(outcome: string): Meta {
  return OUTCOME_META[outcome] ?? { label: outcome, tone: "outline" }
}

export function originMeta(origin: string): Meta {
  return ORIGIN_META[origin] ?? { label: origin, tone: "outline" }
}

export function matchMethodLabel(method: string | null | undefined): string {
  if (!method) return "—"
  return MATCH_METHOD_LABELS[method] ?? method
}

export function skipReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "Skipped"
  return SKIP_REASON_LABELS[reason] ?? reason
}

export function sourceLabel(source: string | null | undefined): string {
  if (!source) return "—"
  return SOURCE_LABELS[source] ?? source
}

/**
 * Tone for the diagnosis block, keyed off `diagnosis.code`.
 *
 * The code exists so a client "can style or group without parsing prose"
 * (`RoutingDiagnosisPublic`'s own docstring). Styling is all it is used for
 * here — the sentence rendered is always `verdict`, never anything chosen from
 * this map. Unknown codes get the neutral tone.
 */
export function diagnosisTone(code: string): "ok" | "warn" | "bad" | "neutral" {
  if (code === "routed" || code === "expected_agent_selected") return "ok"
  if (code === "error" || code === "unavailable") return "bad"
  if (code === "expected_agent_looks_reachable") return "neutral"
  if (code.startsWith("expected_agent_") || code === "no_match") return "warn"
  if (code === "no_candidates" || code === "all_candidates_skipped") return "warn"
  return "neutral"
}

/** Filter options. Values match the backend vocabularies above. */
export const OUTCOME_FILTER_OPTIONS = [
  "no_match",
  "routed",
  "error",
  "parked_install",
] as const

export const ORIGIN_FILTER_OPTIONS = [
  "server_channel",
  "app_mcp",
  "identity",
  "simulate",
] as const

/**
 * Which of the three message-text cases a decision is in.
 *
 * `message_text_notice` is **not** the discriminator, and using it as one is a
 * mistake worth naming: it is set on every row alike while the gate is closed
 * because "it describes the server's current setting, not this row's contents"
 * (`RoutingDecisionPublic`'s own field comment). Branching on it claims the
 * gate hid something for a decision that never carried a message at all.
 *
 * `message_sha256` is the discriminator the API ships for exactly this, and it
 * is returned whatever the gate says: hash present + text NULL means withheld;
 * both NULL means there was no message.
 *
 * Typed structurally so it accepts `RoutingDecisionSummary` and
 * `RoutingDecisionPublic` alike without restating either generated type.
 */
export function messageTextState(row: {
  message_text?: string | null
  message_sha256?: string | null
}): "present" | "withheld" | "absent" {
  if (row.message_text) return "present"
  return row.message_sha256 ? "withheld" : "absent"
}

/**
 * Said when text was withheld but the server sent no notice — i.e. the gate is
 * open now and was closed when this decision was captured, so there is no
 * server-authored sentence for this case. States the mechanical fact only.
 */
export const MESSAGE_WITHHELD_NO_NOTICE =
  "Message text withheld for this decision — only a hash of it is stored."

/** Said when the decision carried no message text to begin with. */
export const MESSAGE_ABSENT =
  "This decision recorded no message text."

export function formatDateTime(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—"
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(1)} s`
}

export function formatConfidence(value: number | null | undefined): string {
  // `null` means the model did not report one (parsed defensively on the
  // backend, plan §8) — that is not the same as 0.0, so it must not render as
  // a number.
  if (value === null || value === undefined) return "—"
  return value.toFixed(2)
}

export function formatSimilarity(value: number): string {
  return Number.isFinite(value) ? value.toFixed(2) : String(value)
}

/** Chrome copy. Nothing here paraphrases a backend verdict. */
export const CARD_DESCRIPTION =
  "Why routing chose the agent it chose — or chose nothing. Read-only: " +
  "nothing on this card edits an agent, a trigger prompt or a bundle."

export const READ_ONLY_BOUNDARY =
  "This card never changes anyone's agent. The only output is wording you " +
  "can send to the agent's owner."

export const SIMULATE_EXPLAINER =
  "Runs one message through routing as the selected user, with no effects — " +
  "no thread binding, no session, no install, no reply. It does spend a real " +
  "LLM call."
