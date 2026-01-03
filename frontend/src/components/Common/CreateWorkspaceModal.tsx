import { useState, useEffect } from "react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import useWorkspace from "@/hooks/useWorkspace"

interface CreateWorkspaceModalProps {
  open: boolean
  onClose: () => void
}

export function CreateWorkspaceModal({ open, onClose }: CreateWorkspaceModalProps) {
  const [workspaceName, setWorkspaceName] = useState("")
  const { createWorkspaceMutation } = useWorkspace()

  // Reset form when modal opens/closes
  useEffect(() => {
    if (!open) {
      setWorkspaceName("")
    }
  }, [open])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!workspaceName.trim()) return

    try {
      await createWorkspaceMutation.mutateAsync({ name: workspaceName.trim() })
      onClose()
    } catch (error) {
      // Error is handled by the mutation's onError callback
      console.error("Failed to create workspace:", error)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-[425px]">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Create New Workspace</DialogTitle>
            <DialogDescription>
              Create a workspace to organize your agents, credentials, and sessions.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="workspace-name">Workspace Name</Label>
              <Input
                id="workspace-name"
                placeholder="e.g., Financial Data, Email Management"
                value={workspaceName}
                onChange={(e) => setWorkspaceName(e.target.value)}
                autoFocus
                required
              />
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!workspaceName.trim() || createWorkspaceMutation.isPending}
            >
              {createWorkspaceMutation.isPending ? "Creating..." : "Create Workspace"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
