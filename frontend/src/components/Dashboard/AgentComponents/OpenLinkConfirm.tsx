import { useCallback, useEffect, useRef, useState } from "react"

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { linkKind } from "./bridgeHost"

/**
 * One confirm at a time, page-wide: a second `ui/open-link` while one is open
 * is rejected ("The link was not opened.") rather than queued (UI spec S4).
 */
let confirmOpen = false

interface Pending {
  url: string
  resolve: (confirmed: boolean) => void
}

/**
 * S4 — "Open example.com in a new tab?" / "Write to ann@example.com?", for a
 * link an html page asked to open. Native `url`s never come here: they are
 * plain links (plan §13 §4).
 *
 * Returns the bridge's `confirmLink` callback and the dialog to render.
 */
export function useOpenLinkConfirm(tileTitle: string) {
  const [pending, setPending] = useState<Pending | null>(null)
  const pendingRef = useRef<Pending | null>(null)

  const confirmLink = useCallback((url: string): Promise<boolean> | null => {
    if (confirmOpen) return null
    confirmOpen = true
    return new Promise<boolean>((resolve) => {
      const next = { url, resolve }
      pendingRef.current = next
      setPending(next)
    })
  }, [])

  const settle = useCallback((confirmed: boolean) => {
    const current = pendingRef.current
    if (!current) return
    pendingRef.current = null
    confirmOpen = false
    setPending(null)
    current.resolve(confirmed)
  }, [])

  // A tile removed while its confirm is open: the page hears a decline and the
  // page-wide slot is freed for the next one.
  useEffect(
    () => () => {
      const current = pendingRef.current
      if (!current) return
      pendingRef.current = null
      confirmOpen = false
      current.resolve(false)
    },
    [],
  )

  const dialog = (
    <OpenLinkConfirm
      pending={pending}
      tileTitle={tileTitle}
      onSettle={settle}
    />
  )
  return { confirmLink, dialog }
}

function describe(url: string): { title: string; action: string } {
  try {
    const parsed = new URL(url)
    if (linkKind(url) === "mail") {
      const address = decodeURIComponent(parsed.pathname) || url
      return { title: `Write to ${address}?`, action: "Write email" }
    }
    return { title: `Open ${parsed.host} in a new tab?`, action: "Open" }
  } catch {
    return { title: "Open this link in a new tab?", action: "Open" }
  }
}

function OpenLinkConfirm({
  pending,
  tileTitle,
  onSettle,
}: {
  pending: Pending | null
  tileTitle: string
  onSettle: (confirmed: boolean) => void
}) {
  const copy = pending ? describe(pending.url) : null
  return (
    <AlertDialog
      open={!!pending}
      onOpenChange={(open) => {
        // Cancel, Escape and an outside click all land here as a decline.
        if (!open) onSettle(false)
      }}
    >
      <AlertDialogContent className="overflow-x-hidden [&>*]:min-w-0">
        <AlertDialogHeader>
          <AlertDialogTitle className="break-words">
            {copy?.title}
          </AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-2">
              <p className="break-all font-mono text-xs">{pending?.url}</p>
              <p className="break-words">Requested by {tileTitle}.</p>
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={(event) => {
              event.preventDefault()
              onSettle(true)
            }}
          >
            {copy?.action}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
