/**
 * The agent's highlights per tile, held in this tab's memory only (plan §7,
 * D-C4). Keyed by block id.
 *
 * - Set by the `dashboard_tile_highlight` socket event (`[]` clears).
 * - Cleared by an owner pick or Escape on a native tile, and by a page's
 *   `setContext` carrying a **new non-empty** selection (the bridge host).
 * - An html tile receives it as `cinna/highlight` only once its document is
 *   ready, and again on each readiness (the bridge host subscribes here).
 * - Lost on a full tab reload — accepted (plan §13 §8).
 *
 * `current` is the native stepper's position ("2/5"); it is UI state, but it
 * lives here so a new highlight resets it in the same write.
 */

import { useSyncExternalStore } from "react"

export interface TileHighlight {
  ids: string[]
  /** Index into `ids` of the stepper's current row; -1 when not stepping yet. */
  current: number
}

const EMPTY: TileHighlight = { ids: [], current: -1 }
const MAX_IDS = 500

const state = new Map<string, TileHighlight>()
const listeners = new Map<string, Set<() => void>>()

function emit(blockId: string) {
  for (const listener of listeners.get(blockId) ?? []) listener()
}

export function getHighlight(blockId: string): TileHighlight {
  return state.get(blockId) ?? EMPTY
}

export function setHighlight(blockId: string, ids: unknown): void {
  if (!Array.isArray(ids)) return
  const clean = ids
    .filter(
      (id): id is string | number =>
        (typeof id === "string" && id !== "") || typeof id === "number",
    )
    .map(String)
    .slice(0, MAX_IDS)
  if (clean.length === 0) {
    clearHighlight(blockId)
    return
  }
  state.set(blockId, { ids: clean, current: 0 })
  emit(blockId)
}

export function clearHighlight(blockId: string): void {
  if (!state.has(blockId)) return
  state.delete(blockId)
  emit(blockId)
}

/** Move the native stepper by `delta`, wrapping at either end. */
export function stepHighlight(blockId: string, delta: 1 | -1): void {
  const current = state.get(blockId)
  if (!current || current.ids.length === 0) return
  const n = current.ids.length
  const next =
    ((((current.current < 0 ? 0 : current.current) + delta) % n) + n) % n
  state.set(blockId, { ...current, current: next })
  emit(blockId)
}

export function subscribeHighlight(
  blockId: string,
  listener: () => void,
): () => void {
  let set = listeners.get(blockId)
  if (!set) {
    set = new Set()
    listeners.set(blockId, set)
  }
  set.add(listener)
  return () => {
    set?.delete(listener)
    if (set?.size === 0) listeners.delete(blockId)
  }
}

export function useTileHighlight(blockId: string): TileHighlight {
  return useSyncExternalStore(
    (listener) => subscribeHighlight(blockId, listener),
    () => getHighlight(blockId),
  )
}
