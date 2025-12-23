import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { AgentsService } from "@/client"
import type { AgentEnvironmentCreate } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Plus } from "lucide-react"

interface AddEnvironmentProps {
  agentId: string
}

export function AddEnvironment({ agentId }: AddEnvironmentProps) {
  const [open, setOpen] = useState(false)
  const [instanceName, setInstanceName] = useState("")
  const [envName, setEnvName] = useState("python-env-basic")
  const [envVersion, setEnvVersion] = useState("1.0.0")
  const [envType, setEnvType] = useState("docker")

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const createMutation = useMutation({
    mutationFn: (data: AgentEnvironmentCreate) =>
      AgentsService.createAgentEnvironment({ id: agentId, requestBody: data }),
    onSuccess: () => {
      showSuccessToast("The new environment has been created successfully.")
      setOpen(false)
      resetForm()
    },
    onError: (error: any) => {
      showErrorToast(error.message || "Failed to create environment")
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["environments", agentId] })
    },
  })

  const resetForm = () => {
    setInstanceName("")
    setEnvName("python-env-basic")
    setEnvVersion("1.0.0")
    setEnvType("docker")
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!instanceName.trim()) {
      showErrorToast("Instance name is required")
      return
    }

    createMutation.mutate({
      instance_name: instanceName,
      env_name: envName,
      env_version: envVersion,
      type: envType,
    })
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button className="gap-2">
          <Plus className="h-4 w-4" />
          Add Environment
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-[500px]">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Create New Environment</DialogTitle>
            <DialogDescription>
              Create a new runtime environment for your agent. This will be a Docker container or
              remote server instance.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="instanceName">Instance Name*</Label>
              <Input
                id="instanceName"
                placeholder="e.g., Production, Testing, Staging"
                value={instanceName}
                onChange={(e) => setInstanceName(e.target.value)}
                required
              />
              <p className="text-sm text-muted-foreground">
                A friendly name to identify this environment
              </p>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="envName">Environment Template</Label>
              <Select value={envName} onValueChange={setEnvName}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="python-env-basic">Python Basic</SelectItem>
                  <SelectItem value="python-env-advanced">Python Advanced</SelectItem>
                  <SelectItem value="python-env-ml">Python ML/AI</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="envVersion">Version</Label>
              <Input
                id="envVersion"
                value={envVersion}
                onChange={(e) => setEnvVersion(e.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="envType">Environment Type</Label>
              <Select value={envType} onValueChange={setEnvType}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="docker">Docker Container</SelectItem>
                  <SelectItem value="remote_ssh">Remote SSH</SelectItem>
                  <SelectItem value="remote_http">Remote HTTP</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={createMutation.isPending}>
              {createMutation.isPending ? "Creating..." : "Create Environment"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
