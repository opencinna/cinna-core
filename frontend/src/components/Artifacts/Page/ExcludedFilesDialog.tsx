import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { plural } from "../artifactFormat"

interface ExcludedFilesDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  revisionNumber: number
  artifactTitle: string
  paths: string[]
}

/** Read-only: the paths a publish left out (dotfiles, secrets, caches). */
export function ExcludedFilesDialog({
  open,
  onOpenChange,
  revisionNumber,
  artifactTitle,
  paths,
}: ExcludedFilesDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle>Files left out</DialogTitle>
          <DialogDescription className="break-words">
            {plural(paths.length, "file")} of revision {revisionNumber} of{" "}
            {artifactTitle} {paths.length === 1 ? "was" : "were"} not published.
          </DialogDescription>
        </DialogHeader>
        <ul className="space-y-0.5 font-mono text-xs text-muted-foreground">
          {paths.map((path) => (
            <li key={path} className="break-all">
              {path}
            </li>
          ))}
        </ul>
      </DialogContent>
    </Dialog>
  )
}
