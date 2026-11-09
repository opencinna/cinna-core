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
import { useUnpublishBrandTheme } from "./brandThemeQueries"

interface UnpublishBrandThemeDialogProps {
  /** The published theme's name. */
  name: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * S6 — confirm taking the server's theme down. Owned by whoever opened it
 * (the published row, in the card or its Sheet), never by a menu item, which would
 * unmount and take the pending state with it.
 */
export function UnpublishBrandThemeDialog({
  name,
  open,
  onOpenChange,
}: UnpublishBrandThemeDialogProps) {
  const unpublish = useUnpublishBrandTheme({
    onSuccess: () => onOpenChange(false),
  })
  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (!unpublish.isPending) onOpenChange(next)
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle className="break-words">
            Unpublish {name}?
          </AlertDialogTitle>
          <AlertDialogDescription>
            Everyone returns to stock Cinna — the sign-in pages and every Cinna
            Desktop on this server. The theme stays as a draft.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={unpublish.isPending}>
            Cancel
          </AlertDialogCancel>
          <AlertDialogAction
            onClick={(e) => {
              e.preventDefault()
              unpublish.mutate()
            }}
            disabled={unpublish.isPending}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            {unpublish.isPending ? "Unpublishing…" : "Unpublish theme"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
