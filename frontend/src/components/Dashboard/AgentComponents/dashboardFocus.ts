/**
 * Tile focus, per-tile context and the dashboard chat panel's registration —
 * the seam between the tiles (P6) and the dashboard chat panel (P7).
 *
 * In memory, per dashboard. **Exactly one** tile is focused per page, and the
 * focus persists until another tile takes it (UI spec S1 "Focus for chat").
 *
 * The panel is optional: the tile asks `hasDashboardChat` before offering Ask
 * and before accepting a page's `ui/message`. Fullscreen never registers one,
 * so a tile there shows no Ask and answers `ui/message` with
 * "No dashboard chat here." — without having to know which route it is on.
 *
 * P7 API:
 * - `useDashboardFocus(dashboardId)` → `{ focusedBlockId, focusSeq, focusTile }`
 * - `useFocusedTileContext(dashboardId)` / `useTileContexts(dashboardId)` —
 *   what each mounted `agent_component` tile shows: title, params, selection,
 *   the page's last `setContext`, data age, prompts.
 * - `registerDashboardChatPanel(dashboardId, handle)` → unregister. `handle.open`
 *   is called by Ask (after focusing the tile); `handle.seedComposer` by a
 *   page's `ui/message` (latest wins; never sends).
 * - `collectTileContext(blockId, timeoutMs)` in `bridgeHost.ts` — the html
 *   page's own answer to `cinna/collect-context`.
 */

import { useSyncExternalStore } from "react"

import type { ComponentPrompt, ComponentView } from "./componentSpec"

/** What a page last passed to `cinna.setContext` (`ui/update-model-context`). */
export interface PageContext {
  summary: string
  selection: string[]
  visible: string[]
}

export interface TileContext {
  blockId: string
  dashboardId: string
  agentId: string
  title: string
  component: string
  skill: string | null
  view: ComponentView | null
  /** The params the source last ran with (`params_resolved`). */
  params: Record<string, unknown>
  /** Native tiles: the owner's picked row/item ids. Html tiles: the page's selection. */
  selection: string[]
  /** Html tiles only: the page's last `setContext`, if any since its `ui/initialize`. */
  pageContext: PageContext | null
  fetchedAt: string | null
  prompts: ComponentPrompt[]
  /** Effective `after_turn` (the tile's override, else the component's). */
  afterTurn: boolean
  /** Whether the tile is on screen right now. */
  visible: boolean
  /** Ask the tile to refresh or re-read, through its own pending state (P7's `after_turn`). */
  refresh: (reason: "after_turn" | "reread") => void
}

export interface DashboardChatPanelHandle {
  /** Open the panel on this tile's agent (Ask, a starter). */
  open: (blockId: string) => void
  /** Fill the composer from a page's `ui/message`; never sends. */
  seedComposer: (text: string, blockId: string) => void
}

interface DashboardState {
  focusedBlockId: string | null
  /** Bumped by every `focusTile`, including a refocus of the same tile. */
  focusSeq: number
  tiles: Map<string, TileContext>
  tileList: TileContext[]
  panel: DashboardChatPanelHandle | null
}

const dashboards = new Map<string, DashboardState>()
const listeners = new Map<string, Set<() => void>>()

function stateOf(dashboardId: string): DashboardState {
  let state = dashboards.get(dashboardId)
  if (!state) {
    state = {
      focusedBlockId: null,
      focusSeq: 0,
      tiles: new Map(),
      tileList: [],
      panel: null,
    }
    dashboards.set(dashboardId, state)
  }
  return state
}

function update(dashboardId: string, patch: Partial<DashboardState>) {
  const next = { ...stateOf(dashboardId), ...patch }
  // Sorted by block id, so the chat's context JSON (and its hash) does not
  // change when a tile merely remounts.
  if (patch.tiles)
    next.tileList = Array.from(patch.tiles.values()).sort((a, b) =>
      a.blockId < b.blockId ? -1 : a.blockId > b.blockId ? 1 : 0,
    )
  dashboards.set(dashboardId, next)
  for (const listener of listeners.get(dashboardId) ?? []) listener()
}

function subscribe(dashboardId: string, listener: () => void) {
  let set = listeners.get(dashboardId)
  if (!set) {
    set = new Set()
    listeners.set(dashboardId, set)
  }
  set.add(listener)
  return () => {
    set?.delete(listener)
  }
}

function useDashboardState<T>(
  dashboardId: string,
  select: (state: DashboardState) => T,
): T {
  return useSyncExternalStore(
    (listener) => subscribe(dashboardId, listener),
    () => select(stateOf(dashboardId)),
  )
}

// ── Focus ────────────────────────────────────────────────────────────────

export function focusTile(dashboardId: string, blockId: string): void {
  // Always written: a refocus of the same tile is an event too (P7 brings a
  // removed context chip back on it), told apart by `focusSeq`.
  update(dashboardId, {
    focusedBlockId: blockId,
    focusSeq: stateOf(dashboardId).focusSeq + 1,
  })
}

export function getFocusedBlockId(dashboardId: string): string | null {
  return stateOf(dashboardId).focusedBlockId
}

export function useDashboardFocus(dashboardId: string) {
  const focusedBlockId = useDashboardState(dashboardId, (s) => s.focusedBlockId)
  const focusSeq = useDashboardState(dashboardId, (s) => s.focusSeq)
  return {
    focusedBlockId,
    focusSeq,
    focusTile: (blockId: string) => focusTile(dashboardId, blockId),
  }
}

export function useIsTileFocused(
  dashboardId: string,
  blockId: string,
): boolean {
  return useDashboardState(dashboardId, (s) => s.focusedBlockId === blockId)
}

// ── Tile context ─────────────────────────────────────────────────────────

export function publishTileContext(context: TileContext): void {
  const tiles = new Map(stateOf(context.dashboardId).tiles)
  tiles.set(context.blockId, context)
  update(context.dashboardId, { tiles })
}

export function patchTileContext(
  dashboardId: string,
  blockId: string,
  patch: Partial<Pick<TileContext, "selection" | "pageContext">>,
): void {
  const current = stateOf(dashboardId).tiles.get(blockId)
  if (!current) return
  publishTileContext({ ...current, ...patch })
}

export function removeTileContext(dashboardId: string, blockId: string): void {
  const state = stateOf(dashboardId)
  if (!state.tiles.has(blockId)) return
  const tiles = new Map(state.tiles)
  tiles.delete(blockId)
  update(dashboardId, {
    tiles,
    focusedBlockId:
      state.focusedBlockId === blockId ? null : state.focusedBlockId,
  })
}

export function getTileContext(
  dashboardId: string,
  blockId: string,
): TileContext | null {
  return stateOf(dashboardId).tiles.get(blockId) ?? null
}

export function useTileContexts(dashboardId: string): TileContext[] {
  return useDashboardState(dashboardId, (s) => s.tileList)
}

export function useFocusedTileContext(dashboardId: string): TileContext | null {
  return useDashboardState(dashboardId, (s) =>
    s.focusedBlockId ? (s.tiles.get(s.focusedBlockId) ?? null) : null,
  )
}

// ── Chat panel ───────────────────────────────────────────────────────────

export function registerDashboardChatPanel(
  dashboardId: string,
  handle: DashboardChatPanelHandle,
): () => void {
  update(dashboardId, { panel: handle })
  return () => {
    if (stateOf(dashboardId).panel === handle)
      update(dashboardId, { panel: null })
  }
}

export function getDashboardChatPanel(
  dashboardId: string,
): DashboardChatPanelHandle | null {
  return stateOf(dashboardId).panel
}

export function useHasDashboardChat(dashboardId: string): boolean {
  return useDashboardState(dashboardId, (s) => s.panel !== null)
}
