import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Clock, CalendarClock, Webhook, Loader2 } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { TaskTriggersApi } from "./triggerApi"
import type { TriggerType } from "./triggerApi"
import { TriggerCard } from "./TriggerCard"
import { AddScheduleTriggerForm } from "./AddScheduleTriggerForm"
import { AddExactDateTriggerForm } from "./AddExactDateTriggerForm"
import { AddWebhookTriggerForm } from "./AddWebhookTriggerForm"

interface TriggerManagementModalProps {
  taskId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function TriggerManagementModal({
  taskId,
  open,
  onOpenChange,
}: TriggerManagementModalProps) {
  const [addingType, setAddingType] = useState<TriggerType | null>(null)

  const { data: triggersData, isLoading } = useQuery({
    queryKey: ["task-triggers", taskId],
    queryFn: () => TaskTriggersApi.listTriggers(taskId),
    enabled: open,
  })

  const triggers = triggersData?.data || []

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Task Triggers</DialogTitle>
        </DialogHeader>

        <div className="space-y-4 max-h-[60vh] overflow-y-auto">
          {/* Adding form */}
          {addingType === "schedule" && (
            <div className="border rounded-lg p-4 bg-accent/30">
              <h3 className="text-sm font-medium mb-3">New Schedule Trigger</h3>
              <AddScheduleTriggerForm
                taskId={taskId}
                onClose={() => setAddingType(null)}
              />
            </div>
          )}

          {addingType === "exact_date" && (
            <div className="border rounded-lg p-4 bg-accent/30">
              <h3 className="text-sm font-medium mb-3">New Exact Date Trigger</h3>
              <AddExactDateTriggerForm
                taskId={taskId}
                onClose={() => setAddingType(null)}
              />
            </div>
          )}

          {addingType === "webhook" && (
            <div className="border rounded-lg p-4 bg-accent/30">
              <h3 className="text-sm font-medium mb-3">New Webhook Trigger</h3>
              <AddWebhookTriggerForm
                taskId={taskId}
                onClose={() => setAddingType(null)}
              />
            </div>
          )}

          {/* Trigger list */}
          {isLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : triggers.length === 0 && !addingType ? (
            <div className="text-center py-8">
              <p className="text-sm text-muted-foreground mb-4">
                No triggers configured. Add a trigger to automate this task.
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              {triggers.map((trigger) => (
                <TriggerCard key={trigger.id} trigger={trigger} taskId={taskId} />
              ))}
            </div>
          )}

          {/* Add trigger buttons */}
          {!addingType && (
            <div className="flex flex-wrap gap-2 pt-2 border-t">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setAddingType("schedule")}
                className="gap-1.5"
              >
                <Clock className="h-3.5 w-3.5" />
                Schedule
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setAddingType("exact_date")}
                className="gap-1.5"
              >
                <CalendarClock className="h-3.5 w-3.5" />
                Exact Date
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setAddingType("webhook")}
                className="gap-1.5"
              >
                <Webhook className="h-3.5 w-3.5" />
                Webhook
              </Button>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
