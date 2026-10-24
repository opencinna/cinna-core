import { useState } from "react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import { Textarea } from "@/components/ui/textarea"

interface BanEntryDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The entry as the admin knows it: the server's or plugin's title. */
  entryLabel: string
  /** What banning does, in one sentence — differs for a server and a plugin. */
  effectSentence: string
  /** The destructive button: "Ban server" / "Ban plugin". */
  confirmLabel: string
  /**
   * Performs the ban. Resolve to close; reject to keep the dialog open with
   * the typed reason (the caller toasts the error and the success).
   */
  onConfirm: (reason: string | null) => Promise<unknown>
  /**
   * Where focus goes when the dialog closes. Radix returns it to the menu
   * trigger that opened it, which a row's pending spinner has replaced.
   */
  returnFocusId?: string
}

/**
 * Take one entry away from every user, optionally saying why (S7). Generic
 * over what is banned: the MCP directory (K4) and the marketplace entries
 * list (K5) pass their own label, effect sentence and button.
 *
 * Not an `AlertDialog`: it carries a field. Banning is reversible, but the
 * effect reaches every user, so it is stated before the button.
 */
export function BanEntryDialog({
  open,
  onOpenChange,
  entryLabel,
  effectSentence,
  confirmLabel,
  onConfirm,
  returnFocusId,
}: BanEntryDialogProps) {
  const [reason, setReason] = useState("")
  const [pending, setPending] = useState(false)

  const handleOpenChange = (next: boolean) => {
    if (pending) return
    if (!next) setReason("")
    onOpenChange(next)
  }

  const confirm = async () => {
    setPending(true)
    try {
      await onConfirm(reason.trim() || null)
      setReason("")
      onOpenChange(false)
    } catch {
      // The caller toasted; the dialog stays with what was typed.
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className="sm:max-w-md"
        onCloseAutoFocus={(e) => {
          const target = returnFocusId
            ? document.getElementById(returnFocusId)
            : null
          if (target) {
            e.preventDefault()
            target.focus()
          }
        }}
        onEscapeKeyDown={(e) => {
          if (pending) e.preventDefault()
        }}
        onInteractOutside={(e) => {
          if (pending) e.preventDefault()
        }}
      >
        <form
          className="grid gap-4"
          onSubmit={(e) => {
            e.preventDefault()
            void confirm()
          }}
        >
          <DialogHeader>
            <DialogTitle className="break-words">
              Ban <em>{entryLabel}</em>?
            </DialogTitle>
            <DialogDescription>{effectSentence}</DialogDescription>
          </DialogHeader>
          <div className="grid gap-1.5">
            <Label htmlFor="ban-entry-reason">Reason (optional)</Label>
            <Textarea
              id="ban-entry-reason"
              maxLength={500}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Shown to other admins"
              rows={3}
              disabled={pending}
            />
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={pending}
              onClick={() => handleOpenChange(false)}
            >
              Cancel
            </Button>
            <LoadingButton
              type="submit"
              variant="destructive"
              loading={pending}
            >
              {pending ? "Banning…" : confirmLabel}
            </LoadingButton>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
