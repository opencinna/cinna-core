import { useForm } from "react-hook-form"
import { useQuery } from "@tanstack/react-query"

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
import { Textarea } from "@/components/ui/textarea"
import { Switch } from "@/components/ui/switch"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import useCustomToast from "@/hooks/useCustomToast"
import { KnowledgeSourcesService, SshKeysService } from "@/client"
import type {
  AIKnowledgeGitRepoPublic,
  Body_update_knowledge_source_api_v1_knowledge_sources__source_id__put as UpdateSourceData,
} from "@/client"
import { Loader2 } from "lucide-react"
import { useState } from "react"

interface EditSourceModalProps {
  source: AIKnowledgeGitRepoPublic
  open: boolean
  onOpenChange: (open: boolean) => void
  onSuccess: () => void
}

export function EditSourceModal({ source, open, onOpenChange, onSuccess }: EditSourceModalProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [isSubmitting, setIsSubmitting] = useState(false)

  const {
    register,
    handleSubmit,
    watch,
    setValue,
    formState: { errors },
  } = useForm<UpdateSourceData>({
    defaultValues: {
      name: source.name,
      description: source.description || "",
      branch: source.branch,
      ssh_key_id: source.ssh_key_id || undefined,
      public_discovery: source.public_discovery || false,
    },
  })

  // Load SSH keys
  const { data: sshKeys } = useQuery({
    queryKey: ["ssh-keys"],
    queryFn: () => SshKeysService.readSshKeys(),
  })

  const onSubmit = async (data: UpdateSourceData) => {
    setIsSubmitting(true)
    try {
      await KnowledgeSourcesService.updateKnowledgeSource({
        sourceId: source.id,
        requestBody: data,
      })
      showSuccessToast("Changes have been saved")
      onSuccess()
    } catch (error: any) {
      showErrorToast(error.message || "Failed to update knowledge source")
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Edit Knowledge Source</DialogTitle>
          <DialogDescription>Update your knowledge source configuration</DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="name">Name *</Label>
            <Input
              id="name"
              placeholder="My Documentation"
              {...register("name", { required: "Name is required" })}
            />
            {errors.name && (
              <p className="text-sm text-destructive">{errors.name.message}</p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="description">Description</Label>
            <Textarea
              id="description"
              placeholder="Description of your knowledge source"
              {...register("description")}
            />
          </div>

          <div className="space-y-2">
            <Label>Git URL</Label>
            <Input
              value={source.git_url}
              disabled
              className="bg-muted"
            />
            <p className="text-xs text-muted-foreground">
              Git URL cannot be changed. Create a new source if you need a different repository.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="branch">Branch</Label>
            <Input
              id="branch"
              placeholder="main"
              {...register("branch")}
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="ssh_key_id">SSH Key (for private repos)</Label>
            <Select
              value={watch("ssh_key_id") || "none"}
              onValueChange={(value) =>
                setValue("ssh_key_id", value === "none" ? undefined : value)
              }
            >
              <SelectTrigger>
                <SelectValue placeholder="None (for public repos)" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">None (for public repos)</SelectItem>
                {sshKeys?.data?.map((key) => (
                  <SelectItem key={key.id} value={key.id}>
                    {key.name} ({key.fingerprint.substring(0, 16)}...)
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex items-center justify-between rounded-lg border p-4">
            <div className="space-y-0.5">
              <Label htmlFor="public_discovery">Public Discovery</Label>
              <p className="text-xs text-muted-foreground">
                Allow other users to discover and enable this knowledge source
              </p>
            </div>
            <Switch
              id="public_discovery"
              checked={watch("public_discovery") || false}
              onCheckedChange={(checked) => setValue("public_discovery", checked)}
            />
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={isSubmitting}>
              {isSubmitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Save Changes
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
