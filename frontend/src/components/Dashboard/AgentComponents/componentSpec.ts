/**
 * Reading an agent component's `component.json` on the client.
 *
 * The generated client types `component_json` as an open object (the kit's
 * schema preserves unknown keys), so every read here narrows from `unknown`.
 * The shapes follow `docs/local_agent_kit/protocols/dashboards.schema.json`
 * (`$defs/component`, `$defs/data`); a field that does not fit is treated as
 * absent rather than trusted.
 */

import type { DashboardComponentPublic } from "@/client"

export type ComponentView = "markdown" | "table" | "list" | "metrics" | "html"

export type ParamType =
  | "enum"
  | "month"
  | "date"
  | "date_range"
  | "text"
  | "number"
  | "boolean"

export interface ParamSpec {
  name: string
  type: ParamType
  label: string
  values: string[]
  default: unknown
}

export interface RefreshDefaults {
  max_age: string
  while_open_every: string | null
  after_turn: boolean
}

export interface ComponentPrompt {
  label: string
  prompt: string
}

export type ComponentSize = "s" | "m" | "l" | "wide" | "tall"

const VIEWS: readonly ComponentView[] = [
  "markdown",
  "table",
  "list",
  "metrics",
  "html",
]
const PARAM_TYPES: readonly ParamType[] = [
  "enum",
  "month",
  "date",
  "date_range",
  "text",
  "number",
  "boolean",
]
const SIZES: readonly ComponentSize[] = ["s", "m", "l", "wide", "tall"]

/** `$defs/size`: on a 12-column grid. */
export const SIZE_GRID: Record<ComponentSize, { w: number; h: number }> = {
  s: { w: 3, h: 2 },
  m: { w: 6, h: 2 },
  l: { w: 6, h: 4 },
  wide: { w: 12, h: 2 },
  tall: { w: 3, h: 4 },
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null
}

function json(component: DashboardComponentPublic | undefined) {
  return isRecord(component?.component_json) ? component.component_json : {}
}

export function readView(
  component: DashboardComponentPublic | undefined,
): ComponentView | null {
  const view = component?.view || json(component).view
  return VIEWS.includes(view as ComponentView) ? (view as ComponentView) : null
}

export function componentTitle(
  component: DashboardComponentPublic | undefined,
): string | null {
  return component?.title || asString(json(component).title) || null
}

export function readParamSpecs(
  component: DashboardComponentPublic | undefined,
): ParamSpec[] {
  const params = json(component).params
  if (!isRecord(params)) return []
  const specs: ParamSpec[] = []
  for (const [name, raw] of Object.entries(params)) {
    if (!isRecord(raw)) continue
    if (!PARAM_TYPES.includes(raw.type as ParamType)) continue
    const values = Array.isArray(raw.values)
      ? raw.values.filter((v): v is string => typeof v === "string")
      : []
    specs.push({
      name,
      type: raw.type as ParamType,
      label: asString(raw.label) || name,
      values,
      default: raw.default,
    })
  }
  return specs
}

/** The component's own refresh rules, with the schema's defaults filled in. */
export function readRefreshDefaults(
  component: DashboardComponentPublic | undefined,
): RefreshDefaults {
  const refresh = json(component).refresh
  const r = isRecord(refresh) ? refresh : {}
  return {
    max_age: asString(r.max_age) || "15m",
    while_open_every: asString(r.while_open_every),
    after_turn: r.after_turn === true,
  }
}

export function readSize(
  component: DashboardComponentPublic | undefined,
): ComponentSize | null {
  const size = json(component).size
  return SIZES.includes(size as ComponentSize) ? (size as ComponentSize) : null
}

export function readPrompts(
  component: DashboardComponentPublic | undefined,
): ComponentPrompt[] {
  const prompts = json(component).prompts
  if (!Array.isArray(prompts)) return []
  return prompts.flatMap((p) => {
    if (!isRecord(p)) return []
    const label = asString(p.label)
    const prompt = asString(p.prompt)
    return label && prompt ? [{ label, prompt }] : []
  })
}

export function readRequiredCredentials(
  component: DashboardComponentPublic | undefined,
): string[] {
  const names = json(component).requires_credentials
  return Array.isArray(names)
    ? names.filter((n): n is string => typeof n === "string" && !!n)
    : []
}

/**
 * The view a cached result was drawn with, guessed from its shape, for the
 * moment before the component itself is readable (its listing needs a running
 * environment; the cached data does not). `html` cannot be told from data.
 */
export function inferView(data: unknown): ComponentView | null {
  if (!isRecord(data)) return null
  if (typeof data.markdown === "string") return "markdown"
  if (Array.isArray(data.columns) && Array.isArray(data.rows)) return "table"
  if (Array.isArray(data.items)) {
    const first = data.items.find(isRecord)
    if (!first) return null
    if ("value" in first && "label" in first) return "metrics"
    if (typeof first.title === "string") return "list"
  }
  return null
}

const UNIT_WORDS: Record<string, [string, string]> = {
  s: ["s", "s"],
  m: ["min", "min"],
  h: ["hour", "hours"],
  d: ["day", "days"],
}

/** "15m" → "15 min", "1h" → "1 hour", "30s" → "30 s". Unknown text passes through. */
export function durationLabel(value: string): string {
  const match = /^([1-9][0-9]*)(s|m|h|d)$/.exec(value)
  if (!match) return value
  const n = Number(match[1])
  const [one, many] = UNIT_WORDS[match[2]]
  return `${n} ${n === 1 ? one : many}`
}

const UNIT_MS: Record<string, number> = {
  s: 1000,
  m: 60_000,
  h: 3_600_000,
  d: 86_400_000,
}

export function durationMs(value: string | null | undefined): number | null {
  if (!value) return null
  const match = /^([1-9][0-9]*)(s|m|h|d)$/.exec(value)
  return match ? Number(match[1]) * UNIT_MS[match[2]] : null
}
