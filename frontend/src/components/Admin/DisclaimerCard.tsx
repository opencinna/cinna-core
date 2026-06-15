import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { ServerConfigService, type ServerConfigUpdate } from "@/client"
import { MarkdownRenderer } from "@/components/Chat/MarkdownRenderer"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { FileText } from "lucide-react"

export function DisclaimerCard() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [editOpen, setEditOpen] = useState(false)
  const [draftMarkdown, setDraftMarkdown] = useState("")

  const { data: config, isLoading } = useQuery({
    queryKey: ["serverConfig"],
    queryFn: () => ServerConfigService.getServerConfig(),
  })

  const updateMutation = useMutation({
    mutationFn: (data: ServerConfigUpdate) =>
      ServerConfigService.updateServerConfig({ requestBody: data }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["serverConfig"] })
      queryClient.invalidateQueries({ queryKey: ["disclaimer"] })
    },
    onError: () => showErrorToast("Failed to update disclaimer"),
  })

  const enabled = config?.disclaimer_enabled ?? false
  const displayMode = config?.disclaimer_display_mode ?? "new_users"
  const markdown = config?.disclaimer_markdown ?? ""

  // Keep the draft in sync with the persisted value whenever the dialog opens.
  useEffect(() => {
    if (editOpen) {
      setDraftMarkdown(markdown)
    }
  }, [editOpen, markdown])

  const handleToggle = (checked: boolean) => {
    updateMutation.mutate({ disclaimer_enabled: checked })
  }

  const handleModeChange = (value: string) => {
    updateMutation.mutate({ disclaimer_display_mode: value })
  }

  const handleSaveMessage = () => {
    updateMutation.mutate(
      { disclaimer_markdown: draftMarkdown },
      {
        onSuccess: () => {
          showSuccessToast("Disclaimer message saved")
          setEditOpen(false)
        },
      },
    )
  }

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-start justify-between">
            <div className="space-y-1.5">
              <CardTitle>Disclaimer</CardTitle>
              <CardDescription>
                Show a server-wide message to users at login, rendered as
                Markdown. Users must acknowledge it before continuing.
              </CardDescription>
            </div>
            <div className="ml-4 mt-1">
              <Switch
                checked={enabled}
                onCheckedChange={handleToggle}
                disabled={isLoading || updateMutation.isPending}
                aria-label="Enable disclaimer"
              />
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium">Disclaimer Message</p>
              <p className="text-xs text-muted-foreground">
                {markdown.trim()
                  ? "Edit the message users must acknowledge"
                  : "No disclaimer message set yet"}
              </p>
            </div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setEditOpen(true)}
              disabled={isLoading}
            >
              <FileText className="h-4 w-4 mr-1.5" />
              Edit
            </Button>
          </div>

          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium">Show disclaimer</p>
              <p className="text-xs text-muted-foreground">
                {displayMode === "every_login"
                  ? "Shown once per browser session"
                  : "Shown once and remembered on the user's browser"}
              </p>
            </div>
            <Select
              value={displayMode}
              onValueChange={handleModeChange}
              disabled={isLoading || updateMutation.isPending}
            >
              <SelectTrigger id="disclaimer-mode" className="w-[150px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="new_users">New User Only</SelectItem>
                <SelectItem value="every_login">Every Login</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent className="sm:max-w-[760px]">
          <DialogHeader>
            <DialogTitle>Edit Disclaimer Message</DialogTitle>
            <DialogDescription>
              Write the disclaimer in Markdown. The live preview shows how users
              will see it.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-2 md:grid-cols-2">
            <div className="grid gap-2">
              <Label htmlFor="disclaimer-markdown">Markdown</Label>
              <Textarea
                id="disclaimer-markdown"
                value={draftMarkdown}
                onChange={(e) => setDraftMarkdown(e.target.value)}
                placeholder="# Welcome&#10;&#10;Please read the following before using this server..."
                className="min-h-[320px] font-mono text-sm"
              />
            </div>
            <div className="grid gap-2">
              <Label>Preview</Label>
              <div className="min-h-[320px] rounded-md border p-3 overflow-y-auto max-h-[320px] prose prose-sm dark:prose-invert max-w-none">
                {draftMarkdown.trim() ? (
                  <MarkdownRenderer
                    content={draftMarkdown}
                    className="[&>h1:first-child]:!mt-0 [&>h2:first-child]:!mt-0 [&>h3:first-child]:!mt-0 [&>h4:first-child]:!mt-0"
                  />
                ) : (
                  <p className="text-sm text-muted-foreground">
                    Nothing to preview yet.
                  </p>
                )}
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleSaveMessage}
              disabled={updateMutation.isPending}
            >
              {updateMutation.isPending ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
