import { MessageSquare, Terminal } from "lucide-react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

export type WebhookType = "session" | "script"

interface WebhookTypeSelectorDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onSelect: (type: WebhookType) => void
}

export function WebhookTypeSelectorDialog({
  open,
  onOpenChange,
  onSelect,
}: WebhookTypeSelectorDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create Webhook</DialogTitle>
          <DialogDescription>
            Choose the type of webhook to create.
          </DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-2 gap-3 py-2">
          <button
            type="button"
            onClick={() => onSelect("session")}
            className="flex flex-col items-start gap-2 p-4 border rounded-lg text-left hover:border-primary hover:bg-accent transition-colors cursor-pointer"
          >
            <div className="flex items-center gap-2">
              <MessageSquare className="h-5 w-5 text-primary" />
              <span className="font-medium text-sm">Session Trigger</span>
            </div>
            <p className="text-xs text-muted-foreground leading-relaxed">
              Starts a new agent session with your prompt + the incoming
              payload. Each call consumes tokens.
            </p>
          </button>

          <button
            type="button"
            onClick={() => onSelect("script")}
            className="flex flex-col items-start gap-2 p-4 border rounded-lg text-left hover:border-primary hover:bg-accent transition-colors cursor-pointer"
          >
            <div className="flex items-center gap-2">
              <Terminal className="h-5 w-5 text-amber-600" />
              <span className="font-medium text-sm">Script Trigger</span>
            </div>
            <p className="text-xs text-muted-foreground leading-relaxed">
              Runs a shell command in the agent&apos;s environment with the
              payload available as <code>$WEBHOOK_PAYLOAD</code>. Only spawns a
              session when needed.
            </p>
          </button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
