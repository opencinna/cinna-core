import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Loader2, Send, FolderSync, RefreshCw } from "lucide-react"

import { AgentSharesService } from "@/client"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Checkbox } from "@/components/ui/checkbox"

interface PushUpdatesModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  agentId: string
  onPushed?: () => void
}

export function PushUpdatesModal({
  open,
  onOpenChange,
  agentId,
  onPushed,
}: PushUpdatesModalProps) {
  const [copyFilesFolder, setCopyFilesFolder] = useState(false)
  const [rebuildEnvironment, setRebuildEnvironment] = useState(false)

  const queryClient = useQueryClient()

  const pushMutation = useMutation({
    mutationFn: () =>
      AgentSharesService.pushUpdatesToClones({
        agentId,
        requestBody: {
          copy_files_folder: copyFilesFolder,
          rebuild_environment: rebuildEnvironment,
        },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agentShares", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agentClones", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agents"] })
      onPushed?.()
      onOpenChange(false)
      // Reset state
      setCopyFilesFolder(false)
      setRebuildEnvironment(false)
    },
  })

  const handleClose = (open: boolean) => {
    if (!open) {
      setCopyFilesFolder(false)
      setRebuildEnvironment(false)
    }
    onOpenChange(open)
  }

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Send className="h-5 w-5" />
            Push Updates to Clones
          </DialogTitle>
          <DialogDescription>
            Push updates to all clones of this agent. Select optional actions below.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="text-sm text-muted-foreground">
            <p>
              This will queue an update for all clones. Clones with automatic updates
              enabled will receive the update when their environment is inactive.
              Manual clones will see "Update Available" and can apply when ready.
            </p>
          </div>

          <div className="space-y-2">
            <div className="font-medium text-sm">Standard Update (always included):</div>
            <ul className="text-sm text-muted-foreground list-disc list-inside space-y-1 pl-1">
              <li>Prompts and system messages</li>
              <li>Scripts and automation logic</li>
              <li>Knowledge base and documentation</li>
              <li>Agent description and configuration</li>
            </ul>
          </div>

          <div className="space-y-4 pt-2 border-t">
            <div className="font-medium text-sm">Optional Actions:</div>

            <div className="space-y-3">
              <div
                className="flex items-start space-x-3 p-3 border rounded-lg hover:bg-muted/50 transition-colors cursor-pointer"
                onClick={() => setCopyFilesFolder(!copyFilesFolder)}
              >
                <Checkbox
                  id="copy-files"
                  checked={copyFilesFolder}
                  onCheckedChange={(checked) => setCopyFilesFolder(checked === true)}
                  onClick={(e) => e.stopPropagation()}
                />
                <div className="flex-1 space-y-1">
                  <div className="flex items-center gap-2 font-medium">
                    <FolderSync className="h-4 w-4 text-blue-500" />
                    Copy Files Folder
                  </div>
                  <p className="text-sm text-muted-foreground">
                    Copy the files folder from the parent agent to all clones.
                    This includes generated reports, caches, and uploaded files.
                  </p>
                </div>
              </div>

              <div
                className="flex items-start space-x-3 p-3 border rounded-lg hover:bg-muted/50 transition-colors cursor-pointer"
                onClick={() => setRebuildEnvironment(!rebuildEnvironment)}
              >
                <Checkbox
                  id="rebuild-env"
                  checked={rebuildEnvironment}
                  onCheckedChange={(checked) => setRebuildEnvironment(checked === true)}
                  onClick={(e) => e.stopPropagation()}
                />
                <div className="flex-1 space-y-1">
                  <div className="flex items-center gap-2 font-medium">
                    <RefreshCw className="h-4 w-4 text-orange-500" />
                    Rebuild Environment
                  </div>
                  <p className="text-sm text-muted-foreground">
                    Rebuild the environment for all clones. Use this when you've
                    updated dependencies or configuration.
                  </p>
                </div>
              </div>
            </div>
          </div>

          {pushMutation.isSuccess && (
            <Alert className="border-green-200 bg-green-50 dark:border-green-800 dark:bg-green-950">
              <AlertDescription className="text-green-800 dark:text-green-200">
                Updates pushed successfully!
              </AlertDescription>
            </Alert>
          )}

          {pushMutation.error && (
            <Alert variant="destructive">
              <AlertDescription>
                {(pushMutation.error as Error).message || "Failed to push updates"}
              </AlertDescription>
            </Alert>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => handleClose(false)}>
            Cancel
          </Button>
          <Button onClick={() => pushMutation.mutate()} disabled={pushMutation.isPending}>
            {pushMutation.isPending ? (
              <>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                Pushing...
              </>
            ) : (
              <>
                <Send className="h-4 w-4 mr-2" />
                Push Updates
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
