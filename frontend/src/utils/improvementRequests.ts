/**
 * Shared vocabulary for Agent Improvement Requests.
 *
 * The backend keeps `status` as a plain VARCHAR validated against a tuple
 * (`IMPROVEMENT_STATUSES`) rather than a Postgres enum, so the generated client
 * types it as `string`. These helpers are the single place the UI turns that
 * string into a label and a colour, and they degrade gracefully on a value the
 * frontend has not been taught yet.
 */

export const IMPROVEMENT_STATUSES = [
  "new",
  "in_progress",
  "completed",
  "declined",
] as const

export type ImprovementStatus = (typeof IMPROVEMENT_STATUSES)[number]

interface StatusMeta {
  label: string
  /** The shared `Badge` variant — no raw palette classes (R14a). */
  variant: "default" | "secondary" | "outline"
  /** Extra classes on top of the variant (the muted "Declined"). */
  className?: string
}

/**
 * Status as a workflow word on a `Badge`: New is the one that wants attention
 * (`default`), In progress is under way (`secondary`), the two closed states
 * are `outline`, Declined also muted. Every badge carries its label, so colour
 * is never the only carrier of meaning. Read by the list row and the detail
 * dialog, so both change together.
 */
const STATUS_META: Record<ImprovementStatus, StatusMeta> = {
  new: { label: "New", variant: "default" },
  in_progress: { label: "In progress", variant: "secondary" },
  completed: { label: "Completed", variant: "outline" },
  declined: {
    label: "Declined",
    variant: "outline",
    className: "text-muted-foreground",
  },
}

const UNKNOWN_STATUS: StatusMeta = {
  label: "Unknown",
  variant: "outline",
  className: "text-muted-foreground",
}

export const getImprovementStatusMeta = (status: string): StatusMeta =>
  STATUS_META[status as ImprovementStatus] ?? {
    ...UNKNOWN_STATUS,
    label: status || UNKNOWN_STATUS.label,
  }

export const getImprovementStatusLabel = (status: string): string =>
  getImprovementStatusMeta(status).label

/**
 * Display form of a request id: its first 8 characters, matching the archive
 * filename (`improvement-<short-id>.zip`) and the CLI's extraction directory.
 * The copy affordance next to it always copies the *full* UUID, because the
 * `/account/improvement-requests/{request_id}` routes parse a whole UUID.
 */
export const improvementShortId = (id: string): string => id.slice(0, 8)

/** The two kinds of request (`AgentImprovementRequest.kind`). */
export const isApiCapabilityRequest = (request: { kind?: string }): boolean =>
  request.kind === "agent_api"

export interface ApiRequestParam {
  name: string
  in: string
  type: string
  description: string
}

export interface ApiRequestResponseField {
  name: string
  type: string
  description: string
}

/** A capability request's payload, every field present (empty when absent). */
export interface ApiCapabilityRequest {
  title: string
  need: string
  method: string
  path: string
  params: ApiRequestParam[]
  responseFields: ApiRequestResponseField[]
  exampleRequest: string
  exampleResponse: string
  specVersionSeen: string
  consumerAgentName: string
  consumerTokenLabel: string
}

type Json = Record<string, unknown>

const obj = (value: unknown): Json =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Json)
    : {}

const str = (value: unknown): string =>
  typeof value === "string" ? value : value == null ? "" : String(value)

const list = (value: unknown): Json[] =>
  Array.isArray(value) ? value.map(obj) : []

/**
 * Read `context.api_request` / `context.consumer` of an agent_api request.
 * The context is caller-authored JSON frozen at submission, so every key is
 * optional here and a wrong type reads as absent rather than throwing.
 */
export function readApiRequest(
  context: Record<string, unknown> | undefined,
): ApiCapabilityRequest {
  const payload = obj(context?.api_request)
  const endpoint = obj(payload.proposed_endpoint)
  const consumer = obj(context?.consumer)
  return {
    title: str(payload.title),
    need: str(payload.need),
    method: str(endpoint.method),
    path: str(endpoint.path),
    params: list(endpoint.params).map((p) => ({
      name: str(p.name),
      in: str(p.in),
      type: str(p.type),
      description: str(p.description),
    })),
    responseFields: list(endpoint.response_fields).map((f) => ({
      name: str(f.name),
      type: str(f.type),
      description: str(f.description),
    })),
    exampleRequest: str(payload.example_request),
    exampleResponse: str(payload.example_response),
    specVersionSeen: str(payload.spec_version_seen),
    consumerAgentName: str(consumer.agent_name),
    consumerTokenLabel: str(consumer.token_label),
  }
}

/** Whether the payload carries anything for the Endpoint tab. */
export const hasEndpointDetails = (request: ApiCapabilityRequest): boolean =>
  Boolean(
    request.method ||
      request.path ||
      request.params.length > 0 ||
      request.responseFields.length > 0 ||
      request.exampleRequest ||
      request.exampleResponse,
  )

/**
 * Session ids that appear on more than one of the given rows. Computed over
 * the rows actually rendered — the marker means "and another one you can see
 * here", which is the only claim a page of rows can support.
 */
export function repeatedSessionIds(
  requests: { session_id?: string | null }[],
): Set<string> {
  const seen = new Set<string>()
  const repeated = new Set<string>()
  for (const { session_id: id } of requests) {
    if (!id) continue
    if (seen.has(id)) repeated.add(id)
    seen.add(id)
  }
  return repeated
}
