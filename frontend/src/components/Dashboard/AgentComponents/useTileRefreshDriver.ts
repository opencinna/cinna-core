import { type RefObject, useEffect, useRef, useState } from "react"

import type { RefreshReason } from "./tileController"

interface Options {
  elementRef: RefObject<HTMLElement | null>
  /**
   * Whether a refresh can be asked for yet (the component is readable, which
   * also means its environment runs). `open` waits for it.
   */
  ready: boolean
  /** `while_open_every` in ms, or `null` for none / Manual. */
  intervalMs: number | null
  onRefresh: (reason: RefreshReason) => void
}

/**
 * The tile's refresh driver (plan §7, D-C6):
 *
 * - the first time the tile is on screen (and `ready`) → `refresh{open}`;
 * - each time it comes back on screen — scrolled into view, or the browser
 *   tab shown again (`visibilitychange`) → `refresh{reshow}`;
 * - `while_open_every` → a timer that runs **only while the tile is on
 *   screen**, posting `refresh{interval}` (the server skips it when the
 *   environment sleeps, so it never wakes one).
 *
 * Returns whether the tile is on screen, which also gates the 30 s re-read.
 */
export function useTileRefreshDriver({
  elementRef,
  ready,
  intervalMs,
  onRefresh,
}: Options): boolean {
  const [inView, setInView] = useState(false)
  const [pageShown, setPageShown] = useState(
    () => document.visibilityState === "visible",
  )
  const visible = inView && pageShown
  const opened = useRef(false)
  const wasVisible = useRef(false)
  const onRefreshRef = useRef(onRefresh)
  onRefreshRef.current = onRefresh

  useEffect(() => {
    const element = elementRef.current
    if (!element) return
    const observer = new IntersectionObserver(
      (entries) => {
        const entry = entries[entries.length - 1]
        if (entry) setInView(entry.isIntersecting)
      },
      { threshold: 0.1 },
    )
    observer.observe(element)
    return () => observer.disconnect()
  }, [elementRef])

  useEffect(() => {
    const onVisibility = () =>
      setPageShown(document.visibilityState === "visible")
    document.addEventListener("visibilitychange", onVisibility)
    return () => document.removeEventListener("visibilitychange", onVisibility)
  }, [])

  useEffect(() => {
    if (!ready) return
    if (visible && !opened.current) {
      opened.current = true
      onRefreshRef.current("open")
    } else if (visible && !wasVisible.current) {
      onRefreshRef.current("reshow")
    }
    wasVisible.current = visible
  }, [visible, ready])

  useEffect(() => {
    if (!ready || !visible || !intervalMs) return
    const timer = setInterval(
      () => onRefreshRef.current("interval"),
      intervalMs,
    )
    return () => clearInterval(timer)
  }, [ready, visible, intervalMs])

  return visible
}
