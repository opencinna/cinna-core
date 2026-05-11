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
import { MessageCircle, Wrench } from "lucide-react"

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
        <CardContent className="space-y-3">
          {conv && (
            <div className="flex items-start gap-3 rounded-md border px-3 py-2.5">
              <div className="flex items-center justify-center w-7 h-7 rounded-lg bg-blue-500/10 shrink-0 mt-0.5">
                <MessageCircle className="h-3.5 w-3.5 text-blue-500" />
              </div>
              <div className="min-w-0 flex-1">
                <p className="text-xs text-muted-foreground mb-0.5">
                  Conversation
                </p>
                <p className="text-sm font-medium">{conv.name}</p>
                <p className="text-xs text-muted-foreground">{conv.type}</p>
              </div>
            </div>
          )}
          {build && (
            <div className="flex items-start gap-3 rounded-md border px-3 py-2.5">
              <div className="flex items-center justify-center w-7 h-7 rounded-lg bg-orange-500/10 shrink-0 mt-0.5">
                <Wrench className="h-3.5 w-3.5 text-orange-500" />
              </div>
              <div className="min-w-0 flex-1">
                <p className="text-xs text-muted-foreground mb-0.5">Building</p>
                <p className="text-sm font-medium">{build.name}</p>
                <p className="text-xs text-muted-foreground">{build.type}</p>
              </div>
            </div>
          )}
          {!conv && !build && (
            <p className="text-sm text-muted-foreground">
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
      <CardContent className="space-y-3">
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
            <div className="flex items-start justify-between gap-3 rounded-md border px-3 py-2.5">
              <div className="flex items-start gap-3 min-w-0 flex-1">
                <div className="flex items-center justify-center w-7 h-7 rounded-lg bg-blue-500/10 shrink-0 mt-0.5">
                  <MessageCircle className="h-3.5 w-3.5 text-blue-500" />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-xs text-muted-foreground mb-1">
                    Conversation
                  </p>
                  <Select
                    value={conversation}
                    onValueChange={handleConvChange}
                  >
                    <SelectTrigger className="h-8">
                      <SelectValue placeholder="Use my defaults" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NONE_VALUE}>
                        Use my defaults
                      </SelectItem>
                      {credentials.map((c) => (
                        <SelectItem key={c.id} value={c.id}>
                          {c.name}{" "}
                          <span className="text-muted-foreground">
                            ({c.type})
                          </span>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </div>

            <div className="flex items-start justify-between gap-3 rounded-md border px-3 py-2.5">
              <div className="flex items-start gap-3 min-w-0 flex-1">
                <div className="flex items-center justify-center w-7 h-7 rounded-lg bg-orange-500/10 shrink-0 mt-0.5">
                  <Wrench className="h-3.5 w-3.5 text-orange-500" />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-xs text-muted-foreground mb-1">
                    Building
                  </p>
                  <Select
                    value={building}
                    onValueChange={handleBuildChange}
                  >
                    <SelectTrigger className="h-8">
                      <SelectValue placeholder="Use my defaults" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NONE_VALUE}>
                        Use my defaults
                      </SelectItem>
                      {credentials.map((c) => (
                        <SelectItem key={c.id} value={c.id}>
                          {c.name}{" "}
                          <span className="text-muted-foreground">
                            ({c.type})
                          </span>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
