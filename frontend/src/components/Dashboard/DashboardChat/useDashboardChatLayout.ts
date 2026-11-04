import { useCallback, useState, useSyncExternalStore } from "react"

const STORAGE_KEY = "dashboardChatOpen"

function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (listener) => {
      const mql = window.matchMedia(query)
      mql.addEventListener("change", listener)
      return () => mql.removeEventListener("change", listener)
    },
    () => window.matchMedia(query).matches,
  )
}

function readStored(): boolean | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY)
    return value === null ? null : value === "true"
  } catch {
    return null
  }
}

/**
 * Where the dashboard chat panel lives and whether it is open (UI spec S5).
 *
 * At ≥ lg it is a docked column, open by default at ≥ xl and closed at lg;
 * the owner's last choice there is remembered for every dashboard. Below lg
 * it is a `Sheet` that always starts closed — a modal sheet that opened by
 * itself on page load would cover the dashboard it is about.
 */
export function useDashboardChatLayout() {
  const docked = useMediaQuery("(min-width: 1024px)")
  const wide = useMediaQuery("(min-width: 1280px)")
  const [stored, setStored] = useState<boolean | null>(readStored)
  const [sheetOpen, setSheetOpen] = useState(false)

  const open = docked ? (stored ?? wide) : sheetOpen
  const setOpen = useCallback(
    (next: boolean) => {
      if (!docked) {
        setSheetOpen(next)
        return
      }
      setStored(next)
      try {
        window.localStorage.setItem(STORAGE_KEY, String(next))
      } catch {
        // Private mode: the choice lasts for this page only.
      }
    },
    [docked],
  )

  return { open, setOpen, docked }
}
