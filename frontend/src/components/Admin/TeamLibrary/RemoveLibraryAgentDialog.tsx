import { AlertCircle } from "lucide-react"
import { useEffect, useRef } from "react"

import type { LibraryAgentAdmin } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
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

interface RemoveLibraryAgentDialogProps {
  /** The agent to remove; `null` closes the dialog. */
  agent: LibraryAgentAdmin | null
  isPending: boolean
  /** The server's sentence when it refused (409); hides the destructive button. */
  refusal: string | null
  onConfirm: () => void
  onClose: () => void
}

/**
 * Confirm for "Remove from team library" (S3). Owned by the page through a
 * `deleteTarget`, so the mutation — and the row's spinner — outlive the menu
 * item that opened it.
 */
export function RemoveLibraryAgentDialog({
  agent,
  isPending,
  refusal,
  onConfirm,
  onClose,
}: RemoveLibraryAgentDialogProps) {
  const name = agent ? agent.name || agent.slug : ""
  const cancelRef = useRef<HTMLButtonElement>(null)

  // A refusal unmounts the focused "Remove agent" button; land on Close
  // rather than letting focus fall back to the dialog container.
  useEffect(() => {
    // Waits for isPending too: a disabled button cannot take focus.
    if (refusal && !isPending) cancelRef.current?.focus()
  }, [refusal, isPending])
  return (
    <AlertDialog
      open={agent !== null}
      onOpenChange={(next) => {
        if (!next && !isPending) onClose()
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle className="break-words">
            Remove {name} from the team library?
          </AlertDialogTitle>
          <AlertDialogDescription>
            People can no longer get it or share changes to it. Copies already
            on their machines are not deleted.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {refusal && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertDescription className="break-words">
              {refusal}
            </AlertDescription>
          </Alert>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel ref={cancelRef} disabled={isPending}>
            {refusal ? "Close" : "Cancel"}
          </AlertDialogCancel>
          {!refusal && (
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                onConfirm()
              }}
              disabled={isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {isPending ? "Removing…" : "Remove agent"}
            </AlertDialogAction>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
