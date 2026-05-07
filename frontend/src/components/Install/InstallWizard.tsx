/**
 * InstallWizard — 4-step bundle install flow.
 *
 * Step 1: Overview — name, publisher, description, bundle metadata.
 * Step 2: Credentials — pick existing or create placeholders for each
 *         ``required_credential_specs`` entry (mirrors the legacy
 *         AcceptShareWizard pattern; backend accepts the same payload).
 * Step 3: AI Credentials — pick the user's default credentials by type.
 * Step 4: Confirm — submit + redirect to the install detail.
 *
 * Adapted from the deleted ``AcceptShareWizard``; the install endpoint
 * accepts the same shape (`{ credentials, ai_credential_selections }`).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useState } from "react"

import {
  CatalogService,
  type AICredentialSelections,
  type CatalogEntryPublic,
} from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { Button } from "@/components/ui/button"

import { WizardStepOverview } from "./WizardStepOverview"
import {
  WizardStepCredentials,
  type CredentialSelection,
} from "./WizardStepCredentials"
import { WizardStepAICredentials } from "./WizardStepAICredentials"
import { WizardStepConfirm } from "./WizardStepConfirm"

type WizardStep = "overview" | "credentials" | "ai_credentials" | "confirm"

interface InstallWizardProps {
  entry: CatalogEntryPublic
}

export function InstallWizard({ entry }: InstallWizardProps) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [currentStep, setCurrentStep] = useState<WizardStep>("overview")
  const [credentialSelections, setCredentialSelections] = useState<
    Record<string, CredentialSelection>
  >({})
  const [aiSelections, setAISelections] = useState<AICredentialSelections>({
    conversation_credential_id: null,
    building_credential_id: null,
  })

  const requiredCredentialSpecs = (entry.required_credential_specs ??
    []) as Array<{ name: string; type: string; allow_sharing?: boolean }>

  const hasCredentialsRequired = requiredCredentialSpecs.length > 0

  const installMutation = useMutation({
    mutationFn: () =>
      CatalogService.installBundle({
        bundleId: entry.bundle_id,
        requestBody: {
          credentials: buildCredentialsPayload(),
          ai_credential_selections:
            aiSelections.conversation_credential_id ||
            aiSelections.building_credential_id
              ? aiSelections
              : null,
        },
      }),
    onSuccess: (install) => {
      queryClient.invalidateQueries({ queryKey: ["agents"] })
      queryClient.invalidateQueries({ queryKey: ["catalog"] })
      showSuccessToast(`Installed ${install.name}`)
      navigate({ to: "/agent/$agentId", params: { agentId: install.id } })
    },
    onError: (e: any) => {
      showErrorToast(e?.body?.detail || "Install failed")
    },
  })

  // Convert credential selections to backend payload
  // ({ credential_name: credential_id_string }).
  function buildCredentialsPayload(): Record<string, string> | null {
    const payload: Record<string, string> = {}
    let hasSelections = false
    for (const [credName, selection] of Object.entries(credentialSelections)) {
      if (
        selection.selectedCredentialId &&
        selection.selectedCredentialId !== "__create_placeholder__"
      ) {
        payload[credName] = selection.selectedCredentialId
        hasSelections = true
      }
    }
    return hasSelections ? payload : null
  }

  const stepOrder: WizardStep[] = ["overview"]
  if (hasCredentialsRequired) stepOrder.push("credentials")
  stepOrder.push("ai_credentials")
  stepOrder.push("confirm")

  const currentIndex = stepOrder.indexOf(currentStep)
  const next = () => {
    if (currentIndex < stepOrder.length - 1) {
      setCurrentStep(stepOrder[currentIndex + 1])
    }
  }
  const back = () => {
    if (currentIndex > 0) setCurrentStep(stepOrder[currentIndex - 1])
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {stepOrder.map((step, idx) => (
          <span
            key={step}
            className={
              "px-2 py-1 rounded " +
              (idx === currentIndex
                ? "bg-primary text-primary-foreground"
                : idx < currentIndex
                ? "bg-muted"
                : "")
            }
          >
            {idx + 1}. {STEP_LABEL[step]}
          </span>
        ))}
      </div>

      {currentStep === "overview" && (
        <WizardStepOverview entry={entry} />
      )}
      {currentStep === "credentials" && (
        <WizardStepCredentials
          requiredSpecs={requiredCredentialSpecs}
          selections={credentialSelections}
          onChange={setCredentialSelections}
        />
      )}
      {currentStep === "ai_credentials" && (
        <WizardStepAICredentials
          selections={aiSelections}
          onChange={setAISelections}
        />
      )}
      {currentStep === "confirm" && (
        <WizardStepConfirm
          entry={entry}
          credentialSelections={credentialSelections}
          aiSelections={aiSelections}
        />
      )}

      <div className="flex justify-between gap-2 pt-2">
        <Button
          variant="outline"
          onClick={back}
          disabled={currentIndex === 0 || installMutation.isPending}
        >
          Back
        </Button>
        {currentStep === "confirm" ? (
          <Button
            onClick={() => installMutation.mutate()}
            disabled={installMutation.isPending}
          >
            {installMutation.isPending ? "Installing..." : "Install"}
          </Button>
        ) : (
          <Button onClick={next}>Next</Button>
        )}
      </div>
    </div>
  )
}

const STEP_LABEL: Record<WizardStep, string> = {
  overview: "Overview",
  credentials: "Credentials",
  ai_credentials: "AI",
  confirm: "Confirm",
}
