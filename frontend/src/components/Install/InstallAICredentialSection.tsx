/**
 * InstallAICredentialSection — AI credential picker on the install page.
 *
 * Two render modes:
 *   - "Provided by publisher" — the bundle ships AI credentials; we
 *     surface a friendly summary and skip the picker entirely.
 *   - "Pick your own" — the user selects their conversation/building AI
 *     credential (or leaves "Use my defaults").
 *
 * Refactored from ``WizardStepAICredentials`` so the wizard files can be
 * deleted in this phase.
 */
import { useQuery } from "@tanstack/react-query"
import { CheckCircle2 } from "lucide-react"

import {
  AiCredentialsService,
  type AICredentialSelections,
  type InstallContextAIPublisherSummaries,
} from "@/client"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

interface InstallAICredentialSectionProps {
  aiProvidedByPublisher: boolean
  aiPublisherSummaries: InstallContextAIPublisherSummaries
  selections: AICredentialSelections
  onChange: (next: AICredentialSelections) => void
}

const NONE_VALUE = "__use_default__"

export function InstallAICredentialSection({
  aiProvidedByPublisher,
  aiPublisherSummaries,
  selections,
  onChange,
}: InstallAICredentialSectionProps) {
  const { data, isLoading } = useQuery({
    queryKey: ["aiCredentialsList"],
    queryFn: () => AiCredentialsService.listAiCredentials(),
    enabled: !aiProvidedByPublisher,
  })
  const credentials = data?.data ?? []

  if (aiProvidedByPublisher) {
    const conv = aiPublisherSummaries.conversation
    const build = aiPublisherSummaries.building
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">AI credentials</CardTitle>
          <CardDescription>
            Provided by the publisher — no action needed and billed to the
            publisher.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          {conv && (
            <div className="flex items-center gap-2">
              <CheckCircle2 className="h-4 w-4 text-emerald-500 shrink-0" />
              <span>
                <span className="font-medium">Conversation:</span> {conv.name}{" "}
                <span className="text-muted-foreground">({conv.type})</span>
              </span>
            </div>
          )}
          {build && (
            <div className="flex items-center gap-2">
              <CheckCircle2 className="h-4 w-4 text-emerald-500 shrink-0" />
              <span>
                <span className="font-medium">Building:</span> {build.name}{" "}
                <span className="text-muted-foreground">({build.type})</span>
              </span>
            </div>
          )}
          {!conv && !build && (
            <p className="text-muted-foreground">
              Publisher AI credential summary is unavailable; the install
              will still link the publisher's keys.
            </p>
          )}
        </CardContent>
      </Card>
    )
  }

  const conversation = selections.conversation_credential_id ?? NONE_VALUE
  const building = selections.building_credential_id ?? NONE_VALUE

  const handleConvChange = (val: string) => {
    onChange({
      ...selections,
      conversation_credential_id: val === NONE_VALUE ? null : val,
    })
  }
  const handleBuildChange = (val: string) => {
    onChange({
      ...selections,
      building_credential_id: val === NONE_VALUE ? null : val,
    })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">AI credentials</CardTitle>
        <CardDescription>
          Pick an AI credential per mode, or leave on "Use my defaults" to
          inherit your default credentials.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading...</p>
        ) : credentials.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            You don't have any AI credentials yet. Add one in Settings -
            AI Credentials before installing, or proceed and the install
            will use placeholders.
          </p>
        ) : (
          <>
            <div className="space-y-1.5">
              <Label>Conversation mode</Label>
              <Select value={conversation} onValueChange={handleConvChange}>
                <SelectTrigger>
                  <SelectValue placeholder="Use my defaults" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE_VALUE}>Use my defaults</SelectItem>
                  {credentials.map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.name}{" "}
                      <span className="text-muted-foreground">({c.type})</span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>Building mode</Label>
              <Select value={building} onValueChange={handleBuildChange}>
                <SelectTrigger>
                  <SelectValue placeholder="Use my defaults" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE_VALUE}>Use my defaults</SelectItem>
                  {credentials.map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.name}{" "}
                      <span className="text-muted-foreground">({c.type})</span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
