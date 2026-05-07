/**
 * Step 4 — Confirm + install.
 *
 * Summarises the selections so the user has one last "are you sure"
 * before we provision their environment.
 */
import { useQuery } from "@tanstack/react-query"

import {
  AiCredentialsService,
  CredentialsService,
  type AICredentialSelections,
  type CatalogEntryPublic,
} from "@/client"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

import type { CredentialSelection } from "./WizardStepCredentials"

interface WizardStepConfirmProps {
  entry: CatalogEntryPublic
  credentialSelections: Record<string, CredentialSelection>
  aiSelections: AICredentialSelections
}

export function WizardStepConfirm({
  entry,
  credentialSelections,
  aiSelections,
}: WizardStepConfirmProps) {
  const { data: credList } = useQuery({
    queryKey: ["credentials"],
    queryFn: () => CredentialsService.readCredentials(),
  })
  const { data: aiList } = useQuery({
    queryKey: ["aiCredentialsList"],
    queryFn: () => AiCredentialsService.listAiCredentials(),
  })

  const credName = (id: string | null | undefined) => {
    if (!id) return "Use my defaults"
    return aiList?.data.find((c) => c.id === id)?.name ?? "(unknown)"
  }
  const userCredName = (id: string | null | undefined) => {
    if (!id) return null
    return credList?.data.find((c) => c.id === id)?.name ?? "(unknown)"
  }

  const credSpecs = (entry.required_credential_specs ?? []) as Array<{
    name: string
  }>

  return (
    <Card>
      <CardHeader>
        <CardTitle>Confirm install</CardTitle>
        <CardDescription>
          Review your selections and click Install. We'll provision the agent
          environment and seed your per-user app-data volume.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <div>
          <p className="text-xs text-muted-foreground mb-1">Bundle</p>
          <p className="font-medium">
            {entry.display_name}{" "}
            {entry.latest_revision_number !== null && (
              <span className="text-muted-foreground">
                v{entry.latest_revision_number}
              </span>
            )}
          </p>
        </div>

        {credSpecs.length > 0 && (
          <div>
            <p className="text-xs text-muted-foreground mb-1">Credentials</p>
            <ul className="space-y-1">
              {credSpecs.map((spec) => {
                const sel = credentialSelections[spec.name]
                const id = sel?.selectedCredentialId
                const isPlaceholder = !id || id === "__create_placeholder__"
                return (
                  <li key={spec.name}>
                    <span className="font-medium">{spec.name}</span> —{" "}
                    {isPlaceholder ? (
                      <span className="text-muted-foreground">
                        Placeholder will be created
                      </span>
                    ) : (
                      <span>{userCredName(id) ?? id}</span>
                    )}
                  </li>
                )
              })}
            </ul>
          </div>
        )}

        <div>
          <p className="text-xs text-muted-foreground mb-1">AI credentials</p>
          <ul className="space-y-1">
            <li>
              <span className="font-medium">Conversation</span> —{" "}
              {credName(aiSelections.conversation_credential_id)}
            </li>
            <li>
              <span className="font-medium">Building</span> —{" "}
              {credName(aiSelections.building_credential_id)}
            </li>
          </ul>
        </div>
      </CardContent>
    </Card>
  )
}
