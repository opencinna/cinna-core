import { useRef } from "react"

import type { McpDirectoryEntryPublic } from "@/client"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import { useDeleteEntry } from "./mcpDirectoryApi"

/**
 * Confirm deleting one server. Owned by the list (`deleteTarget`): the menu
 * item that asks for it unmounts on select. Pending stays on the button and
 * the dialog cannot be dismissed mid-request.
 */
export function DeleteMcpServerDialog({
  entry,
  onClose,
  onReturnFocus,
}: {
  entry: McpDirectoryEntryPublic
  onClose: () => void
  /**
   * Where focus goes on close: the menu trigger that opened the confirm has
   * been replaced by the row's spinner, and after a delete the row is gone.
   */
  onReturnFocus: (deleted: boolean) => void
}) {
  const mutation = useDeleteEntry()
  // Set by the call's own success, so close-time focus does not depend on
  // when the observer's `isSuccess` updates.
  const deletedRef = useRef(false)
  const pending = mutation.isPending

  return (
    <AlertDialog
      open
      onOpenChange={(open) => {
        if (!open && !pending) onClose()
      }}
    >
      <AlertDialogContent
        onCloseAutoFocus={(e) => {
          e.preventDefault()
          onReturnFocus(deletedRef.current)
        }}
        onEscapeKeyDown={(e) => {
          if (pending) e.preventDefault()
        }}
      >
        <AlertDialogHeader>
          <AlertDialogTitle className="break-words">
            Delete MCP server <em>{entry.title}</em>?
          </AlertDialogTitle>
          <AlertDialogDescription>
            Users can no longer add it; agents that already use it keep their
            copy.
            {entry.source === "opencinna" &&
              " It was imported from the opencinna index, so the next import brings it back — ban it instead to keep it out."}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <LoadingButton
            variant="destructive"
            loading={pending}
            onClick={() =>
              mutation.mutate(
                { entry },
                {
                  onSuccess: () => {
                    deletedRef.current = true
                    onClose()
                  },
                },
              )
            }
          >
            {pending ? "Deleting…" : "Delete server"}
          </LoadingButton>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
