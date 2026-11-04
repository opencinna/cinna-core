/**
 * The `<dashboard_context>` JSON the panel sends with a message (plan §5.2, §8).
 *
 * `{dashboard, focused{tile_id, component, skill, title, params, selection,
 * data_age, summary?, selected_text?, context?}, also_yours[], other_tiles[]}`
 *
 * - `focused.tile_id` is the block id; the backend's tile tools match on it.
 * - `summary` ≤ 8,000 and `selected_text` ≤ 2,000 are applied here, and the
 *   whole text stays ≤ 12,000 (the server refuses more): `other_tiles` go
 *   first, then `also_yours`, then the summary is cut.
 * - The backend sends the block only when it changed, so nothing here may
 *   change on its own between two messages: the data's age is sent as the
 *   moment it was fetched, not as "3 minutes ago".
 */

import { parseTimestamp } from "@/components/Common/RelativeTime"
import type { CollectedContext } from "@/components/Dashboard/AgentComponents/bridgeHost"
import type { TileContext } from "@/components/Dashboard/AgentComponents/dashboardFocus"

export const CONTEXT_MAX_CHARS = 12_000
export const SUMMARY_MAX_CHARS = 8_000
export const SELECTED_TEXT_MAX_CHARS = 2_000
const MAX_SUMMARY_LINES = 40

// Control characters except tab and newline (the server strips the same set).
// biome-ignore lint/suspicious/noControlCharactersInRegex: matching them is the point.
const CONTROL_RE = /[\x00-\x08\x0b-\x1f\x7f-\x9f]/g

export function stripControl(text: string): string {
  return text.replace(CONTROL_RE, "")
}

function typeName(type: string | undefined): string {
  if (!type) return ""
  const parts = type.split(/[/#]/).filter(Boolean)
  return parts[parts.length - 1] ?? type
}

/**
 * A page that never called `setContext`, as text: its title, then one line
 * per microdata item (`Type id: key=value, …`), the selected items first,
 * at most 40 lines plus "(40 of N shown)".
 */
export function collectedSummary(collected: CollectedContext): string {
  const selected = new Set(collected.selection)
  const items = [...collected.microdata].sort(
    (a, b) =>
      Number(!!b.id && selected.has(b.id)) -
      Number(!!a.id && selected.has(a.id)),
  )
  const lines = items.map((item) => {
    const head = [typeName(item.type), item.id].filter(Boolean).join(" ")
    const props = Object.entries(item.properties)
      .filter(([, value]) => value !== null && value !== "")
      .map(([key, value]) => `${key}=${value}`)
      .join(", ")
    return head ? `${head}: ${props}` : props
  })
  const shown = lines.slice(0, MAX_SUMMARY_LINES)
  const out = [collected.summary, ...shown].filter(Boolean)
  if (lines.length > shown.length)
    out.push(`(${shown.length} of ${lines.length} shown)`)
  return out.join("\n")
}

export interface FocusedView {
  summary: string | null
  selection: string[]
  selectedText: string | null
}

/** What the owner sees on the focused tile: the page's own answer first. */
export function focusedView(
  tile: TileContext,
  collected: CollectedContext | null,
): FocusedView {
  if (collected) {
    const quote = stripControl(collected.selected_text || "").trim()
    return {
      summary:
        collected.source === "set_context"
          ? collected.summary || null
          : collectedSummary(collected) || null,
      selection: collected.selection,
      selectedText: quote ? quote.slice(0, SELECTED_TEXT_MAX_CHARS) : null,
    }
  }
  if (tile.pageContext) {
    return {
      summary: tile.pageContext.summary || null,
      selection: tile.pageContext.selection,
      selectedText: null,
    }
  }
  return { summary: null, selection: tile.selection, selectedText: null }
}

/** `key=value` pairs of the params that carry a value. */
export function paramPairs(params: Record<string, unknown>): string[] {
  return Object.entries(params)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
}

function utcStamp(value: string | null): string | null {
  if (!value) return null
  return parseTimestamp(value)?.toISOString() ?? null
}

interface BuildOptions {
  dashboard: { id: string; name: string }
  focused: TileContext
  view: FocusedView
  /** The component's own `context` (how to read the view), sent once per chat. */
  componentContext: string | null
  tiles: TileContext[]
  agentName: (agentId: string) => string
}

export function buildDashboardContext({
  dashboard,
  focused,
  view,
  componentContext,
  tiles,
  agentName,
}: BuildOptions): string {
  const focusedBlock: Record<string, unknown> = {
    tile_id: focused.blockId,
    component: focused.component,
    skill: focused.skill,
    title: focused.title,
    params: focused.params,
    selection: view.selection,
    data_age: utcStamp(focused.fetchedAt),
  }
  if (view.summary)
    focusedBlock.summary = stripControl(view.summary).slice(
      0,
      SUMMARY_MAX_CHARS,
    )
  if (view.selectedText) focusedBlock.selected_text = view.selectedText
  if (componentContext) focusedBlock.context = componentContext

  const others = tiles.filter((t) => t.blockId !== focused.blockId)
  const payload: Record<string, unknown> = {
    dashboard,
    focused: focusedBlock,
    also_yours: others
      .filter((t) => t.agentId === focused.agentId)
      .map((t) => ({
        tile_id: t.blockId,
        component: t.component,
        title: t.title,
        params: t.params,
      })),
    other_tiles: others
      .filter((t) => t.agentId !== focused.agentId)
      .map((t) => ({ title: t.title, agent: agentName(t.agentId) })),
  }

  let text = JSON.stringify(payload)
  if (text.length > CONTEXT_MAX_CHARS) {
    payload.other_tiles = []
    text = JSON.stringify(payload)
  }
  if (text.length > CONTEXT_MAX_CHARS) {
    payload.also_yours = []
    text = JSON.stringify(payload)
  }
  // Escaping makes a character cost up to 6 in JSON, so cut and re-measure.
  while (
    text.length > CONTEXT_MAX_CHARS &&
    typeof focusedBlock.summary === "string" &&
    focusedBlock.summary
  ) {
    const over = text.length - CONTEXT_MAX_CHARS
    focusedBlock.summary = focusedBlock.summary.slice(
      0,
      Math.max(0, focusedBlock.summary.length - over - 16),
    )
    text = JSON.stringify(payload)
  }
  // Still over: give up the least essential facts one by one before the
  // caller has to drop the whole block.
  if (text.length > CONTEXT_MAX_CHARS && "context" in focusedBlock) {
    delete focusedBlock.context
    text = JSON.stringify(payload)
  }
  if (text.length > CONTEXT_MAX_CHARS && "selected_text" in focusedBlock) {
    delete focusedBlock.selected_text
    text = JSON.stringify(payload)
  }
  let selection = view.selection
  while (text.length > CONTEXT_MAX_CHARS && selection.length > 0) {
    selection = selection.slice(0, Math.floor(selection.length / 2))
    focusedBlock.selection = selection
    text = JSON.stringify(payload)
  }
  return text
}

/** The components a session's earlier messages already explained (`context` sent). */
export function componentsAlreadyExplained(
  messages: Array<{ message_metadata?: unknown }>,
): Set<string> {
  const seen = new Set<string>()
  for (const message of messages) {
    const metadata = message.message_metadata
    if (!metadata || typeof metadata !== "object") continue
    const raw = (metadata as Record<string, unknown>).dashboard_context
    if (typeof raw !== "string") continue
    try {
      const focused = (
        JSON.parse(raw) as {
          focused?: { component?: unknown; context?: unknown }
        }
      ).focused
      if (focused?.context && typeof focused.component === "string")
        seen.add(focused.component)
    } catch {
      // Not ours to judge: the server stored it.
    }
  }
  return seen
}
