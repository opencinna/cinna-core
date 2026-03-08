import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { WebappInterfaceConfigService } from "@/client"
import type { AgentWebappInterfaceConfigUpdate } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Loader2, MessageCircle, Hammer, CircleOff } from "lucide-react"

interface WebappInterfaceModalProps {
  agentId: string
  open: boolean
  onClose: () => void
}

export function WebappInterfaceModal({
  agentId,
  open,
  onClose,
}: WebappInterfaceModalProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: config, isLoading } = useQuery({
    queryKey: ["webapp-interface-config", agentId],
    queryFn: () =>
      WebappInterfaceConfigService.getWebappInterfaceConfig({ agentId }),
    enabled: open,
  })

  const updateMutation = useMutation({
    mutationFn: (data: AgentWebappInterfaceConfigUpdate) =>
      WebappInterfaceConfigService.updateWebappInterfaceConfig({
        agentId,
        requestBody: data,
      }),
    onSuccess: () => {
      showSuccessToast("Interface settings updated")
      queryClient.invalidateQueries({
        queryKey: ["webapp-interface-config", agentId],
      })
    },
    onError: (error: any) => {
      showErrorToast(
        error?.body?.detail || "Failed to update interface settings"
      )
    },
  })

  const handleToggle = (field: "show_header", value: boolean) => {
    updateMutation.mutate({ [field]: value })
  }

  const handleChatModeChange = (value: string) => {
    updateMutation.mutate({
      chat_mode: value === "disabled" ? null : value,
    })
  }

  const currentChatMode = config?.chat_mode ?? null

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Web App Interface</DialogTitle>
          <DialogDescription>
            Configure the appearance of your web app for all share links.
          </DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label htmlFor="webapp-show-header">Show Header</Label>
                <p className="text-xs text-muted-foreground">
                  Display the header bar with agent name at the top of the web
                  app
                </p>
              </div>
              <Switch
                id="webapp-show-header"
                checked={config?.show_header ?? true}
                onCheckedChange={(v) => handleToggle("show_header", v)}
                disabled={updateMutation.isPending}
              />
            </div>

            <div className="space-y-3">
              <Label>Chat</Label>
              <p className="text-xs text-muted-foreground">
                Allow webapp viewers to chat with the agent
              </p>
              <RadioGroup
                value={currentChatMode ?? "disabled"}
                onValueChange={handleChatModeChange}
                disabled={updateMutation.isPending}
              >
                <div
                  className={`flex items-start space-x-3 p-3 border rounded-lg transition-colors ${
                    currentChatMode === null
                      ? "border-muted-foreground/30 bg-muted/30"
                      : "border-border hover:bg-muted/50"
                  }`}
                >
                  <RadioGroupItem value="disabled" id="chat-disabled" className="mt-0.5" />
                  <div className="flex items-center gap-2">
                    <CircleOff className="h-4 w-4 text-muted-foreground" />
                    <Label htmlFor="chat-disabled" className="font-medium cursor-pointer">
                      Disabled
                    </Label>
                  </div>
                </div>

                <div
                  className={`flex items-start space-x-3 p-3 border rounded-lg transition-colors ${
                    currentChatMode === "conversation"
                      ? "border-blue-500 bg-blue-500/5 dark:bg-blue-950/20"
                      : "border-border hover:bg-muted/50"
                  }`}
                >
                  <RadioGroupItem value="conversation" id="chat-conversation" className="mt-0.5" />
                  <div>
                    <div className="flex items-center gap-2">
                      <MessageCircle className="h-4 w-4 text-blue-500" />
                      <Label htmlFor="chat-conversation" className="font-medium cursor-pointer">
                        Conversation
                      </Label>
                    </div>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      Viewers interact with the UI — change filters, switch views
                    </p>
                  </div>
                </div>

                <div
                  className={`flex items-start space-x-3 p-3 border rounded-lg transition-colors ${
                    currentChatMode === "building"
                      ? "border-orange-500 bg-orange-500/5 dark:bg-orange-950/20"
                      : "border-border hover:bg-muted/50"
                  }`}
                >
                  <RadioGroupItem value="building" id="chat-building" className="mt-0.5" />
                  <div>
                    <div className="flex items-center gap-2">
                      <Hammer className="h-4 w-4 text-orange-500" />
                      <Label htmlFor="chat-building" className="font-medium cursor-pointer">
                        Building
                      </Label>
                    </div>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      Viewers can create and modify widgets
                    </p>
                  </div>
                </div>
              </RadioGroup>
            </div>
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
