import { useState } from "react"
import { useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { z } from "zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import type { UserDashboardBlockPublic } from "@/client"
import { DashboardsService } from "@/client"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog"
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"

const editBlockSchema = z.object({
  title: z.string().max(255).optional().or(z.literal("")),
  show_border: z.boolean(),
  show_header: z.boolean(),
})

type EditBlockFormData = z.infer<typeof editBlockSchema>

interface EditBlockDialogProps {
  block: UserDashboardBlockPublic
  dashboardId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function EditBlockDialog({ block, dashboardId, open, onOpenChange }: EditBlockDialogProps) {
  const queryClient = useQueryClient()

  const blockConfig = block.config as Record<string, string> | null
  const [filePath, setFilePath] = useState<string>(blockConfig?.file_path ?? "")

  const form = useForm<EditBlockFormData>({
    resolver: zodResolver(editBlockSchema),
    defaultValues: {
      title: block.title ?? "",
      show_border: block.show_border,
      show_header: block.show_header,
    },
  })

  const showHeader = form.watch("show_header")

  // Fetch available files from the agent's default environment (files/ subfolder)
  const { data: availableFiles, isLoading: filesLoading } = useQuery({
    queryKey: ["blockEnvFiles", dashboardId, block.id],
    queryFn: () => DashboardsService.listBlockEnvFiles({
      dashboardId,
      blockId: block.id,
      subfolder: "files",
    }),
    enabled: block.view_type === "agent_env_file" && open,
  })

  // Prefix with 'files/' for full workspace-relative paths
  const files = (availableFiles ?? []).map((f) => `files/${f}`)

  const updateBlockMutation = useMutation({
    mutationFn: (data: EditBlockFormData) => {
      const config =
        block.view_type === "agent_env_file"
          ? { file_path: filePath }
          : block.config

      return DashboardsService.updateBlock({
        dashboardId,
        blockId: block.id,
        requestBody: {
          title: data.title || null,
          show_border: data.show_border,
          show_header: data.show_header,
          config,
        },
      })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["userDashboard", dashboardId] })
      onOpenChange(false)
    },
  })

  const onSubmit = (data: EditBlockFormData) => {
    updateBlockMutation.mutate(data)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Edit Block</DialogTitle>
        </DialogHeader>

        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
            <FormField
              control={form.control}
              name="show_header"
              render={({ field }) => (
                <FormItem className="flex items-center justify-between py-1">
                  <FormLabel className="mb-0">Show header</FormLabel>
                  <FormControl>
                    <Switch
                      checked={field.value}
                      onCheckedChange={field.onChange}
                    />
                  </FormControl>
                </FormItem>
              )}
            />

            {showHeader && (
              <FormField
                control={form.control}
                name="title"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Custom Title (optional)</FormLabel>
                    <FormControl>
                      <Input
                        placeholder="Defaults to agent name"
                        {...field}
                      />
                    </FormControl>
                  </FormItem>
                )}
              />
            )}

            <FormField
              control={form.control}
              name="show_border"
              render={({ field }) => (
                <FormItem className="flex items-center justify-between py-1">
                  <FormLabel className="mb-0">Show border</FormLabel>
                  <FormControl>
                    <Switch
                      checked={field.value}
                      onCheckedChange={field.onChange}
                    />
                  </FormControl>
                </FormItem>
              )}
            />

            {/* Agent Env File: file selector */}
            {block.view_type === "agent_env_file" && (
              <div className="flex items-center gap-4 pt-1">
                <Label className="w-24 shrink-0 text-right text-sm">File</Label>
                <Select
                  value={filePath}
                  onValueChange={setFilePath}
                  disabled={filesLoading}
                >
                  <SelectTrigger className="ml-auto w-56">
                    <SelectValue
                      placeholder={
                        filesLoading
                          ? "Loading files..."
                          : files.length === 0
                          ? "No files found"
                          : "Select a file"
                      }
                    />
                  </SelectTrigger>
                  <SelectContent>
                    {files.map((file) => (
                      <SelectItem key={file} value={file}>
                        {file}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}

            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={updateBlockMutation.isPending}>
                Save Changes
              </Button>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
