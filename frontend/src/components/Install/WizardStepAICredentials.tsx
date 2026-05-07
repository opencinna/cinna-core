/**
 * Step 3 — AI credentials.
 *
 * The bundle revision doesn't tell us which SDK type the install needs
 * yet (the install endpoint will use the user's defaults if neither
 * dropdown is set). We surface the user's available credentials grouped
 * by type and let them optionally pin one for conversation / building
 * mode.
 */
import { useQuery } from "@tanstack/react-query"

import { AiCredentialsService, type AICredentialSelections } from "@/client"
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

interface WizardStepAICredentialsProps {
  selections: AICredentialSelections
  onChange: (next: AICredentialSelections) => void
}

const NONE_VALUE = "__use_default__"

export function WizardStepAICredentials({
  selections,
  onChange,
}: WizardStepAICredentialsProps) {
  const { data, isLoading } = useQuery({
    queryKey: ["aiCredentialsList"],
    queryFn: () => AiCredentialsService.listAiCredentials(),
  })
  const credentials = data?.data ?? []

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
        <CardTitle>AI credentials</CardTitle>
        <CardDescription>
          Optional — pick a specific AI credential for each mode, or leave on
          "Use my defaults" to inherit the credentials marked as default.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading...</p>
        ) : credentials.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            You don't have any AI credentials yet. Add one in Settings → AI
            Credentials before installing, or proceed and the install will use
            placeholders.
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
