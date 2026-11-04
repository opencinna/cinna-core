/**
 * The `config` of an `agent_component` block, and the error body its routes
 * answer with — both read from `unknown` because the generated client types
 * them as open objects.
 *
 * Config shape (backend `AgentComponentBlockConfig`, `extra=forbid`):
 * `{component, skill?, params, refresh: {max_age?, while_open_every?, after_turn?}}`.
 * Error body (backend `_dashboard_component_errors.py`):
 * `{error: {code, message, retry_after_s?, field?}}`.
 */

import type {
  DashboardComponentErrorDetail,
  UserDashboardBlockPublic,
} from "@/client"
import { isRecord } from "./componentSpec"

export interface RefreshOverrides {
  max_age?: string | null
  /** A duration of at least 1m, or `"manual"` for "never while open". */
  while_open_every?: string | null
  after_turn?: boolean | null
}

export interface AgentComponentConfig {
  component: string
  skill: string | null
  params: Record<string, unknown>
  refresh: RefreshOverrides
}

export function readTileConfig(
  block: Pick<UserDashboardBlockPublic, "config">,
): AgentComponentConfig {
  const config = isRecord(block.config) ? block.config : {}
  const refresh = isRecord(config.refresh) ? config.refresh : {}
  return {
    component: typeof config.component === "string" ? config.component : "",
    skill:
      typeof config.skill === "string" && config.skill ? config.skill : null,
    params: isRecord(config.params) ? { ...config.params } : {},
    refresh: {
      max_age: typeof refresh.max_age === "string" ? refresh.max_age : null,
      while_open_every:
        typeof refresh.while_open_every === "string"
          ? refresh.while_open_every
          : null,
      after_turn:
        typeof refresh.after_turn === "boolean" ? refresh.after_turn : null,
    },
  }
}

const CODES: ReadonlyArray<DashboardComponentErrorDetail["code"]> = [
  "rate_limited",
  "needs_credentials",
  "call_failed",
  "invalid_input",
  "not_found",
  "env_unavailable",
  "adapter_unsupported",
]

/** The `{error:{…}}` detail of a failed component route call, or `null`. */
export function readComponentError(
  error: unknown,
): DashboardComponentErrorDetail | null {
  const body = isRecord(error) ? error.body : undefined
  const detail = isRecord(body) ? body.error : undefined
  if (!isRecord(detail)) return null
  if (!CODES.includes(detail.code as DashboardComponentErrorDetail["code"]))
    return null
  return {
    code: detail.code as DashboardComponentErrorDetail["code"],
    message:
      typeof detail.message === "string" && detail.message
        ? detail.message
        : "The request failed.",
    retry_after_s:
      typeof detail.retry_after_s === "number" ? detail.retry_after_s : null,
    field:
      typeof detail.field === "string" && detail.field ? detail.field : null,
  }
}

/** One sentence for any failure: the component error's message, else the API's. */
export function componentErrorMessage(
  error: unknown,
  fallback: string,
): string {
  const detail = readComponentError(error)
  if (detail) return detail.message
  const body = isRecord(error) ? error.body : undefined
  const message = isRecord(body) ? body.detail : undefined
  return typeof message === "string" && message ? message : fallback
}
