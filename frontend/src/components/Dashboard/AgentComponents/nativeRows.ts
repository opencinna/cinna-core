import { type KeyboardEvent, type MouseEvent, useEffect, useRef } from "react"

import { cn } from "@/lib/utils"

/**
 * Selection and highlight paint for a native list/table row (UI spec S1):
 * selected → `bg-primary/20`; highlighted → `bg-primary/10` plus a left bar, so a
 * row that is both still shows both; the stepper's current row also gets an
 * inset ring and is scrolled into view.
 */
export function rowStateClass(state: {
  selectable: boolean
  selected: boolean
  highlighted: boolean
  current: boolean
}) {
  // Never the hover token (`--accent` equals `--muted`), and each paint
  // carries its own `hover:` so the row just clicked keeps it under the pointer.
  return cn(
    state.selectable && "cursor-pointer",
    state.highlighted &&
      "bg-primary/10 hover:bg-primary/10 shadow-[inset_3px_0_0_0_var(--primary)]",
    state.selected && "bg-primary/20 hover:bg-primary/20",
    state.current && "ring-1 ring-inset ring-primary",
  )
}

/** Scrolls the stepper's current row into view when it becomes current. */
export function useScrollIntoViewWhen<T extends HTMLElement>(active: boolean) {
  const ref = useRef<T | null>(null)
  useEffect(() => {
    if (active) ref.current?.scrollIntoView({ block: "nearest" })
  }, [active])
  return ref
}

/**
 * Click and Enter/Space toggle a row's selection — except on its title link,
 * which only follows the link (UI spec S1 "Selection").
 */
export function rowToggleHandlers(onToggle: () => void) {
  return {
    onClick: (event: MouseEvent<HTMLElement>) => {
      if ((event.target as HTMLElement).closest("a")) return
      onToggle()
    },
    onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
      if (event.target !== event.currentTarget) return
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault()
        onToggle()
      }
    },
  }
}

/** Native `url`s: http, https and mailto only, drawn as plain links, no confirm. */
export function isSafeUrl(value: unknown): value is string {
  return typeof value === "string" && /^(https?:\/\/|mailto:)/i.test(value)
}

export function rowId(value: unknown): string | null {
  if (typeof value === "string" && value) return value
  if (typeof value === "number") return String(value)
  return null
}
