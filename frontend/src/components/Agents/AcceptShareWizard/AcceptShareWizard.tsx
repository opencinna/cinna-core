import { useState } from "react"
import { useMutation } from "@tanstack/react-query"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { AgentSharesService } from "@/client"
import type { PendingSharePublic } from "@/client"

import { WizardStepOverview } from "./WizardStepOverview"
import { WizardStepAICredentials } from "./WizardStepAICredentials"
import { WizardStepCredentials } from "./WizardStepCredentials"
import { WizardStepConfirm } from "./WizardStepConfirm"

interface AcceptShareWizardProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  share: PendingSharePublic
  onComplete: () => void
}

interface AICredentialSelections {
  conversationCredentialId: string | null
  buildingCredentialId: string | null
}

type WizardStep = "overview" | "ai_credentials" | "credentials" | "confirm"

export function AcceptShareWizard({
  open,
  onOpenChange,
  share,
  onComplete,
}: AcceptShareWizardProps) {
  const [currentStep, setCurrentStep] = useState<WizardStep>("overview")
  const [credentialsData, setCredentialsData] = useState<
    Record<string, Record<string, string>>
  >({})
  const [aiCredentialSelections, setAICredentialSelections] = useState<AICredentialSelections>({
    conversationCredentialId: null,
    buildingCredentialId: null,
  })

  const acceptMutation = useMutation({
    mutationFn: () =>
      AgentSharesService.acceptShare({
        shareId: share.id,
        requestBody: {
          credentials: credentialsData,
          ai_credential_selections: aiCredentialSelections.conversationCredentialId || aiCredentialSelections.buildingCredentialId
            ? {
                conversation_credential_id: aiCredentialSelections.conversationCredentialId || undefined,
                building_credential_id: aiCredentialSelections.buildingCredentialId || undefined,
              }
            : undefined,
        },
      }),
    onSuccess: () => {
      onComplete()
      resetWizard()
    },
  })

  const resetWizard = () => {
    setCurrentStep("overview")
    setCredentialsData({})
    setAICredentialSelections({
      conversationCredentialId: null,
      buildingCredentialId: null,
    })
  }

  const needsCredentialSetup =
    share.credentials_required?.some((c) => !c.allow_sharing) ?? false

  // Determine if AI credentials step is needed
  // Skip if owner provided AI credentials OR if no AI credential types required
  const needsAICredentialStep =
    !share.ai_credentials_provided &&
    (share.required_ai_credential_types?.length ?? 0) > 0

  const handleNext = () => {
    if (currentStep === "overview") {
      if (needsAICredentialStep) {
        setCurrentStep("ai_credentials")
      } else if (needsCredentialSetup) {
        setCurrentStep("credentials")
      } else {
        setCurrentStep("confirm")
      }
    } else if (currentStep === "ai_credentials") {
      if (needsCredentialSetup) {
        setCurrentStep("credentials")
      } else {
        setCurrentStep("confirm")
      }
    } else if (currentStep === "credentials") {
      setCurrentStep("confirm")
    }
  }

  const handleBack = () => {
    if (currentStep === "confirm") {
      if (needsCredentialSetup) {
        setCurrentStep("credentials")
      } else if (needsAICredentialStep) {
        setCurrentStep("ai_credentials")
      } else {
        setCurrentStep("overview")
      }
    } else if (currentStep === "credentials") {
      if (needsAICredentialStep) {
        setCurrentStep("ai_credentials")
      } else {
        setCurrentStep("overview")
      }
    } else if (currentStep === "ai_credentials") {
      setCurrentStep("overview")
    }
  }

  const handleCredentialsChange = (
    data: Record<string, Record<string, string>>
  ) => {
    setCredentialsData(data)
  }

  const handleAICredentialsChange = (selections: AICredentialSelections) => {
    setAICredentialSelections(selections)
  }

  const handleAccept = () => {
    acceptMutation.mutate()
  }

  const handleClose = () => {
    onOpenChange(false)
    resetWizard()
  }

  // Calculate step numbers
  const totalSteps = 1 + (needsAICredentialStep ? 1 : 0) + (needsCredentialSetup ? 1 : 0) + 1 // overview + ai_creds? + creds? + confirm
  let currentStepNumber = 1
  if (currentStep === "ai_credentials") {
    currentStepNumber = 2
  } else if (currentStep === "credentials") {
    currentStepNumber = needsAICredentialStep ? 3 : 2
  } else if (currentStep === "confirm") {
    currentStepNumber = totalSteps
  }

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            Accept Shared Agent: {share.original_agent_name}
          </DialogTitle>
        </DialogHeader>

        {/* Step indicator */}
        <div className="flex items-center justify-center gap-2 py-4">
          <StepIndicator
            step={1}
            label="Overview"
            active={currentStep === "overview"}
            completed={currentStepNumber > 1}
          />
          {needsAICredentialStep && (
            <>
              <StepConnector />
              <StepIndicator
                step={2}
                label="AI Credentials"
                active={currentStep === "ai_credentials"}
                completed={currentStepNumber > 2}
              />
            </>
          )}
          {needsCredentialSetup && (
            <>
              <StepConnector />
              <StepIndicator
                step={needsAICredentialStep ? 3 : 2}
                label="Credentials"
                active={currentStep === "credentials"}
                completed={currentStepNumber > (needsAICredentialStep ? 3 : 2)}
              />
            </>
          )}
          <StepConnector />
          <StepIndicator
            step={totalSteps}
            label="Confirm"
            active={currentStep === "confirm"}
            completed={false}
          />
        </div>

        {/* Step content */}
        {currentStep === "overview" && (
          <WizardStepOverview
            share={share}
            onNext={handleNext}
            onCancel={handleClose}
          />
        )}

        {currentStep === "ai_credentials" && (
          <WizardStepAICredentials
            share={share}
            aiCredentialSelections={aiCredentialSelections}
            onChange={handleAICredentialsChange}
            onNext={handleNext}
            onBack={handleBack}
          />
        )}

        {currentStep === "credentials" && (
          <WizardStepCredentials
            share={share}
            credentialsData={credentialsData}
            onChange={handleCredentialsChange}
            onNext={handleNext}
            onBack={handleBack}
          />
        )}

        {currentStep === "confirm" && (
          <WizardStepConfirm
            share={share}
            credentialsData={credentialsData}
            onAccept={handleAccept}
            onBack={handleBack}
            isLoading={acceptMutation.isPending}
            error={
              acceptMutation.error instanceof Error
                ? acceptMutation.error.message
                : acceptMutation.error
                  ? String(acceptMutation.error)
                  : undefined
            }
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function StepIndicator({
  step,
  label,
  active,
  completed,
}: {
  step: number
  label: string
  active: boolean
  completed: boolean
}) {
  return (
    <div
      className={`flex items-center gap-2 ${active ? "text-primary" : "text-muted-foreground"}`}
    >
      <div
        className={`
        w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium
        ${active ? "bg-primary text-primary-foreground" : completed ? "bg-primary/20 text-primary" : "bg-muted"}
      `}
      >
        {step}
      </div>
      <span className="text-sm hidden sm:inline">{label}</span>
    </div>
  )
}

function StepConnector() {
  return <span className="mx-2 text-muted-foreground">-</span>
}
