import type { ReactNode } from "react"

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

interface ArtifactConfirmDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: ReactNode
  description: ReactNode
  /** The destructive button names the effect ("Delete artifact"). */
  actionLabel: string
  pendingLabel: string
  pending: boolean
  onConfirm: () => void
  /** A refusal that keeps the dialog open (a 409), in the server's words. */
  error?: string | null
}

/**
 * The S8 confirmations (delete artifact, delete revision, revoke link): an
 * `AlertDialog` naming the entity, one destructive button, held open while the
 * request runs so its pending state has somewhere to live (§6 Pending).
 */
export function ArtifactConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  actionLabel,
  pendingLabel,
  pending,
  onConfirm,
  error,
}: ArtifactConfirmDialogProps) {
  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (!pending) onOpenChange(next)
      }}
    >
      <AlertDialogContent className="[&>*]:min-w-0">
        <AlertDialogHeader>
          <AlertDialogTitle className="break-words">{title}</AlertDialogTitle>
          <AlertDialogDescription className="break-words">
            {description}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error && (
          <p className="text-sm text-destructive break-words" role="alert">
            {error}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={(e) => {
              e.preventDefault()
              onConfirm()
            }}
            disabled={pending}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            {pending ? pendingLabel : actionLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
