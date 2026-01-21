import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import type { PendingSharePublic } from "@/client"
import { Info, Shield, Pencil, Check, Wrench, MessageCircle } from "lucide-react"

interface WizardStepOverviewProps {
  share: PendingSharePublic
  onNext: () => void
  onCancel: () => void
}

export function WizardStepOverview({
  share,
  onNext,
  onCancel,
}: WizardStepOverviewProps) {
  const isBuilder = share.share_mode === "builder"

  return (
    <div className="space-y-6">
      {/* Agent info */}
      <div className="p-4 bg-muted rounded-lg">
        <h3 className="font-semibold mb-2">{share.original_agent_name}</h3>
        <p className="text-sm text-muted-foreground">
          {share.original_agent_description || "No description provided"}
        </p>
        <div className="mt-3 text-sm">
          <span className="text-muted-foreground">Shared by: </span>
          <span className="font-medium">{share.shared_by_email}</span>
          {share.shared_by_name && (
            <span className="text-muted-foreground">
              {" "}
              ({share.shared_by_name})
            </span>
          )}
        </div>
      </div>

      {/* Access mode explanation */}
      <div className="space-y-3">
        <h4 className="font-medium flex items-center gap-2">
          <Shield className="h-4 w-4" />
          Your Access Level
        </h4>

        <Badge
          variant={isBuilder ? "default" : "secondary"}
          className="text-sm"
        >
          {isBuilder ? (
            <>
              <Wrench className="h-3 w-3 mr-1" />
              Builder Access
            </>
          ) : (
            <>
              <MessageCircle className="h-3 w-3 mr-1" />
              Conversation Access
            </>
          )}
        </Badge>

        <div className="text-sm space-y-2 text-muted-foreground">
          {isBuilder ? (
            <>
              <p className="flex items-center gap-2">
                <Pencil className="h-4 w-4 text-green-500 shrink-0" />
                You can modify prompts, scripts, and configuration
              </p>
              <p className="flex items-center gap-2">
                <Check className="h-4 w-4 text-green-500 shrink-0" />
                Full access to building mode
              </p>
              <p className="flex items-center gap-2">
                <Info className="h-4 w-4 text-yellow-500 shrink-0" />
                You can still receive updates from the owner
              </p>
            </>
          ) : (
            <>
              <p className="flex items-center gap-2">
                <Check className="h-4 w-4 text-green-500 shrink-0" />
                You can use the agent in conversation mode
              </p>
              <p className="flex items-center gap-2">
                <Info className="h-4 w-4 text-blue-500 shrink-0" />
                Configuration is read-only (you can view but not edit)
              </p>
              <p className="flex items-center gap-2">
                <Check className="h-4 w-4 text-green-500 shrink-0" />
                Interface settings can be customized
              </p>
            </>
          )}
        </div>
      </div>

      {/* What you'll get */}
      <div className="space-y-2">
        <h4 className="font-medium flex items-center gap-2">
          <Info className="h-4 w-4" />
          What you'll receive
        </h4>
        <ul className="text-sm text-muted-foreground list-disc list-inside space-y-1">
          <li>Your own copy of the agent (independent sessions)</li>
          <li>All prompts and scripts from the original</li>
          <li>Knowledge base files</li>
          <li>Updates when the owner pushes changes</li>
        </ul>
      </div>

      {/* Actions */}
      <div className="flex justify-end gap-3 pt-4 border-t">
        <Button variant="outline" onClick={onCancel}>
          Cancel
        </Button>
        <Button onClick={onNext}>Continue</Button>
      </div>
    </div>
  )
}
