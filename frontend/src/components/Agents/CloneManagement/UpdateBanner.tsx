import { useState } from "react"
import { RefreshCw, X, Loader2 } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog"

interface UpdateBannerProps {
  pendingSince?: string
  onApply: () => void
  onDismiss?: () => void
  isLoading: boolean
  isDismissing?: boolean
}

export function UpdateBanner({
  pendingSince,
  onApply,
  onDismiss,
  isLoading,
  isDismissing = false
}: UpdateBannerProps) {
  const [showDismissDialog, setShowDismissDialog] = useState(false)

  const handleDismiss = () => {
    onDismiss?.()
    setShowDismissDialog(false)
  }

  return (
    <>
      <Alert className="mb-4 border-blue-200 bg-blue-50 dark:border-blue-800 dark:bg-blue-950">
        <RefreshCw className="h-4 w-4 text-blue-600 dark:text-blue-400" />
        <AlertTitle className="text-blue-800 dark:text-blue-200">Update Available</AlertTitle>
        <AlertDescription className="flex items-center justify-between">
          <span className="text-blue-700 dark:text-blue-300">
            New changes from the parent agent are available.
            {pendingSince && ` Last updated: ${new Date(pendingSince).toLocaleString()}`}
          </span>
          <div className="flex items-center gap-2 ml-4">
            {onDismiss && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setShowDismissDialog(true)}
                disabled={isLoading || isDismissing}
                className="text-blue-700 hover:text-blue-800 dark:text-blue-300 dark:hover:text-blue-200"
              >
                <X className="h-4 w-4 mr-1" />
                Dismiss
              </Button>
            )}
            <Button
              size="sm"
              onClick={onApply}
              disabled={isLoading || isDismissing}
            >
              Apply Update
            </Button>
          </div>
        </AlertDescription>
      </Alert>

      <Dialog open={showDismissDialog} onOpenChange={setShowDismissDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Dismiss Update?</DialogTitle>
            <DialogDescription>
              Are you sure you want to dismiss this update?
            </DialogDescription>
          </DialogHeader>
          <div className="text-sm text-muted-foreground">
            <p>
              Dismissing will remove all pending update requests from the parent agent.
              You can still receive new updates when the owner pushes them again.
            </p>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowDismissDialog(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleDismiss}
              disabled={isDismissing}
            >
              {isDismissing ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Dismissing...
                </>
              ) : (
                "Dismiss Update"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
