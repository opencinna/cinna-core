import type { AdminAgentEnvironmentPublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Badge } from "@/components/ui/badge"

interface AdminEnvBulkRebuildDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  selectedEnvs: AdminAgentEnvironmentPublic[]
  onConfirm: () => void
  isPending: boolean
}

export function AdminEnvBulkRebuildDialog({
  open,
  onOpenChange,
  selectedEnvs,
  onConfirm,
  isPending,
}: AdminEnvBulkRebuildDialogProps) {
  // Group by template
  const byTemplate = selectedEnvs.reduce(
    (acc, env) => {
      const key = env.env_name
      if (!acc[key]) acc[key] = []
      acc[key].push(env)
      return acc
    },
    {} as Record<string, AdminAgentEnvironmentPublic[]>
  )

  const runningCount = selectedEnvs.filter((e) => e.status === "running").length
  const stoppedCount = selectedEnvs.filter((e) => e.status === "stopped").length
  const suspendedCount = selectedEnvs.filter(
    (e) => e.status === "suspended"
  ).length
  const otherCount = selectedEnvs.length - runningCount - stoppedCount - suspendedCount

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>
            Rebuild {selectedEnvs.length} environment
            {selectedEnvs.length !== 1 ? "s" : ""}?
          </DialogTitle>
          <DialogDescription>
            Each environment will be rebuilt in the background. Status updates
            will appear in real time. Running environments will be restarted
            automatically; stopped and suspended environments will remain in
            their current state after rebuild.
          </DialogDescription>
        </DialogHeader>

        {/* Status split summary */}
        <div className="flex flex-wrap gap-2 text-sm">
          {runningCount > 0 && (
            <span className="inline-flex items-center gap-1">
              <span className="h-2 w-2 rounded-full bg-emerald-500" />
              <strong>{runningCount}</strong> running (will restart)
            </span>
          )}
          {stoppedCount > 0 && (
            <span className="inline-flex items-center gap-1">
              <span className="h-2 w-2 rounded-full bg-neutral-400" />
              <strong>{stoppedCount}</strong> stopped
            </span>
          )}
          {suspendedCount > 0 && (
            <span className="inline-flex items-center gap-1">
              <span className="h-2 w-2 rounded-full bg-slate-400" />
              <strong>{suspendedCount}</strong> suspended
            </span>
          )}
          {otherCount > 0 && (
            <span className="text-muted-foreground">
              +{otherCount} other
            </span>
          )}
        </div>

        {/* Grouped by template */}
        <div className="max-h-60 overflow-y-auto space-y-3">
          {Object.entries(byTemplate).map(([templateName, envs]) => (
            <div key={templateName}>
              <div className="flex items-center gap-2 mb-1">
                <Badge variant="outline" className="text-xs font-mono">
                  {templateName}
                </Badge>
                <span className="text-xs text-muted-foreground">
                  {envs.length} env{envs.length !== 1 ? "s" : ""}
                </span>
              </div>
              <ul className="pl-2 space-y-0.5">
                {envs.map((env) => (
                  <li
                    key={env.id}
                    className="text-xs text-muted-foreground truncate"
                  >
                    {env.agent_name} / {env.instance_name}{" "}
                    <span className="text-muted-foreground/60">
                      ({env.owner_email})
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={isPending}
          >
            Cancel
          </Button>
          <Button onClick={onConfirm} disabled={isPending}>
            {isPending ? "Queueing..." : `Rebuild ${selectedEnvs.length} environment${selectedEnvs.length !== 1 ? "s" : ""}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
