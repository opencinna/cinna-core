import { AlertCircle } from "lucide-react"
import { useEffect, useRef } from "react"

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
import {
  type CatalogItemRef,
  catalogKindOf,
  catalogNoun,
} from "@/utils/catalogItems"

const CONSEQUENCE = {
  desktop:
    "People can no longer get it or share changes to it. Copies already on their computers are not deleted.",
  cloud: "People can no longer install it. This cannot be undone.",
  skill: "Agents can no longer add it. This cannot be undone.",
} as const

interface DeleteCatalogItemDialogProps {
  /** The item to delete; `null` closes the dialog. */
  item: CatalogItemRef | null
  isPending: boolean
  /** The server's sentence when it refused (409); replaces the destructive button. */
  refusal: string | null
  onConfirm: () => void
  onClose: () => void
}

/**
 * S3b — confirm for deleting a catalog item. Owned by whoever opened it
 * through a target (`deleteTarget`), so the mutation — and a row's spinner —
 * outlive the menu item. Rebuilt from the team library's remove confirm.
 */
export function DeleteCatalogItemDialog({
  item,
  isPending,
  refusal,
  onConfirm,
  onClose,
}: DeleteCatalogItemDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null)

  // A refusal unmounts the focused destructive button; land on Close rather
  // than letting focus fall back to the dialog container. Waits for
  // `isPending` too: a disabled button cannot take focus.
  useEffect(() => {
    if (refusal && !isPending) cancelRef.current?.focus()
  }, [refusal, isPending])

  return (
    <AlertDialog
      open={item !== null}
      onOpenChange={(next) => {
        if (!next && !isPending) onClose()
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle className="break-words">
            Delete {item?.display_name} from the catalog?
          </AlertDialogTitle>
          <AlertDialogDescription>
            {item ? CONSEQUENCE[catalogKindOf(item)] : null}
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
              {isPending
                ? "Deleting…"
                : `Delete ${item ? catalogNoun(item) : ""}`}
            </AlertDialogAction>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
