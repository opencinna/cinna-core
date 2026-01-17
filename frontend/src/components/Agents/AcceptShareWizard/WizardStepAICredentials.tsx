import { useQuery } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { AiCredentialsService } from "@/client"
import type { PendingSharePublic } from "@/client"
import { CheckCircle, AlertCircle, Key } from "lucide-react"

interface AICredentialSelections {
  conversationCredentialId: string | null
  buildingCredentialId: string | null
}

interface WizardStepAICredentialsProps {
  share: PendingSharePublic
  aiCredentialSelections: AICredentialSelections
  onChange: (selections: AICredentialSelections) => void
  onNext: () => void
  onBack: () => void
}

export function WizardStepAICredentials({
  share,
  aiCredentialSelections,
  onChange,
  onNext,
  onBack,
}: WizardStepAICredentialsProps) {
  // Get required AI credential types from share
  const requiredTypes = share.required_ai_credential_types || []

  // Fetch user's AI credentials
  const { data: aiCredentials } = useQuery({
    queryKey: ["aiCredentialsList"],
    queryFn: () => AiCredentialsService.listAiCredentials(),
    enabled: !share.ai_credentials_provided && requiredTypes.length > 0,
  })

  // Get unique required SDK types
  const conversationType = requiredTypes.find(r => r.purpose === "conversation")
  const buildingType = requiredTypes.find(r => r.purpose === "building")

  // Filter credentials by type
  const conversationCredentials = aiCredentials?.data.filter(
    c => c.type === conversationType?.sdk_type
  ) || []
  const buildingCredentials = aiCredentials?.data.filter(
    c => c.type === buildingType?.sdk_type
  ) || []

  // Find default credentials
  const defaultConvCred = conversationCredentials.find(c => c.is_default)
  const defaultBuildCred = buildingCredentials.find(c => c.is_default)

  // If AI credentials are provided by owner, show that
  if (share.ai_credentials_provided) {
    return (
      <div className="space-y-6">
        <p className="text-sm text-muted-foreground">
          The agent owner is providing their AI credentials for this agent.
        </p>

        <div className="space-y-3">
          <h4 className="font-medium text-green-700 flex items-center gap-2">
            <CheckCircle className="h-4 w-4" />
            AI Credentials Provided
          </h4>
          <div className="space-y-2">
            {share.conversation_ai_credential_name && (
              <div className="flex items-center justify-between p-3 bg-green-50 dark:bg-green-950/20 rounded-lg border border-green-200 dark:border-green-800">
                <div className="flex items-center gap-2">
                  <Key className="h-4 w-4 text-green-600" />
                  <span className="font-medium">Conversation Mode</span>
                  <span className="text-muted-foreground">-</span>
                  <span>{share.conversation_ai_credential_name}</span>
                </div>
                <Badge className="bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200">
                  Provided by owner
                </Badge>
              </div>
            )}
            {share.building_ai_credential_name && (
              <div className="flex items-center justify-between p-3 bg-green-50 dark:bg-green-950/20 rounded-lg border border-green-200 dark:border-green-800">
                <div className="flex items-center gap-2">
                  <Key className="h-4 w-4 text-green-600" />
                  <span className="font-medium">Building Mode</span>
                  <span className="text-muted-foreground">-</span>
                  <span>{share.building_ai_credential_name}</span>
                </div>
                <Badge className="bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200">
                  Provided by owner
                </Badge>
              </div>
            )}
          </div>
        </div>

        {/* Actions */}
        <div className="flex justify-between pt-4 border-t">
          <Button variant="outline" onClick={onBack}>
            Back
          </Button>
          <Button onClick={onNext}>Continue</Button>
        </div>
      </div>
    )
  }

  // Check if user has defaults or selections for required types
  const hasConversationCred =
    aiCredentialSelections.conversationCredentialId ||
    defaultConvCred ||
    !conversationType
  const hasBuildingCred =
    aiCredentialSelections.buildingCredentialId ||
    defaultBuildCred ||
    !buildingType
  const canContinue = hasConversationCred && hasBuildingCred

  return (
    <div className="space-y-6">
      <p className="text-sm text-muted-foreground">
        This agent requires AI credentials to function. Select which credentials to use,
        or your defaults will be applied.
      </p>

      {/* Required AI credential types */}
      {requiredTypes.length > 0 && (
        <div className="space-y-4">
          {/* Conversation credential selection */}
          {conversationType && (
            <div className="space-y-2 p-4 bg-muted/50 rounded-lg border">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Key className="h-4 w-4" />
                  <span className="font-medium">Conversation Mode</span>
                </div>
                {defaultConvCred && !aiCredentialSelections.conversationCredentialId && (
                  <Badge variant="outline" className="text-xs text-green-600">
                    Using default: {defaultConvCred.name}
                  </Badge>
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                Requires: {conversationType.sdk_type}
              </p>
              <Label htmlFor="conv-cred">Select Credential</Label>
              <Select
                value={aiCredentialSelections.conversationCredentialId || ""}
                onValueChange={(v) =>
                  onChange({
                    ...aiCredentialSelections,
                    conversationCredentialId: v || null,
                  })
                }
              >
                <SelectTrigger id="conv-cred">
                  <SelectValue placeholder={defaultConvCred ? `Use default (${defaultConvCred.name})` : "Select credential..."} />
                </SelectTrigger>
                <SelectContent>
                  {conversationCredentials.length === 0 ? (
                    <div className="py-2 px-2 text-sm text-muted-foreground">
                      No {conversationType.sdk_type} credentials found
                    </div>
                  ) : (
                    conversationCredentials.map((cred) => (
                      <SelectItem key={cred.id} value={cred.id}>
                        {cred.name} {cred.is_default && "(default)"}
                      </SelectItem>
                    ))
                  )}
                </SelectContent>
              </Select>
              {conversationCredentials.length === 0 && (
                <p className="text-sm text-destructive flex items-center gap-1">
                  <AlertCircle className="h-3 w-3" />
                  No credentials available. Please add one in Settings first.
                </p>
              )}
            </div>
          )}

          {/* Building credential selection (only if different from conversation) */}
          {buildingType && buildingType.sdk_type !== conversationType?.sdk_type && (
            <div className="space-y-2 p-4 bg-muted/50 rounded-lg border">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Key className="h-4 w-4" />
                  <span className="font-medium">Building Mode</span>
                </div>
                {defaultBuildCred && !aiCredentialSelections.buildingCredentialId && (
                  <Badge variant="outline" className="text-xs text-green-600">
                    Using default: {defaultBuildCred.name}
                  </Badge>
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                Requires: {buildingType.sdk_type}
              </p>
              <Label htmlFor="build-cred">Select Credential</Label>
              <Select
                value={aiCredentialSelections.buildingCredentialId || ""}
                onValueChange={(v) =>
                  onChange({
                    ...aiCredentialSelections,
                    buildingCredentialId: v || null,
                  })
                }
              >
                <SelectTrigger id="build-cred">
                  <SelectValue placeholder={defaultBuildCred ? `Use default (${defaultBuildCred.name})` : "Select credential..."} />
                </SelectTrigger>
                <SelectContent>
                  {buildingCredentials.length === 0 ? (
                    <div className="py-2 px-2 text-sm text-muted-foreground">
                      No {buildingType.sdk_type} credentials found
                    </div>
                  ) : (
                    buildingCredentials.map((cred) => (
                      <SelectItem key={cred.id} value={cred.id}>
                        {cred.name} {cred.is_default && "(default)"}
                      </SelectItem>
                    ))
                  )}
                </SelectContent>
              </Select>
              {buildingCredentials.length === 0 && (
                <p className="text-sm text-destructive flex items-center gap-1">
                  <AlertCircle className="h-3 w-3" />
                  No credentials available. Please add one in Settings first.
                </p>
              )}
            </div>
          )}
        </div>
      )}

      {requiredTypes.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No specific AI credentials are required for this agent.
          Your default credentials will be used.
        </p>
      )}

      {/* Actions */}
      <div className="flex justify-between pt-4 border-t">
        <Button variant="outline" onClick={onBack}>
          Back
        </Button>
        <Button onClick={onNext} disabled={!canContinue}>
          Continue
        </Button>
      </div>
    </div>
  )
}
