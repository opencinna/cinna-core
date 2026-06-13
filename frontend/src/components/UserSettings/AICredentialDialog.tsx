import { useState, useEffect, useRef } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Loader2, Info, CheckCircle2 } from "lucide-react"

import {
  AiCredentialsService,
  AICredentialPublic,
  AICredentialTestResult,
  AICredentialType,
} from "@/client"
import { AnthropicCredentialsModal } from "./AnthropicCredentialsModal"
import { AffectedEnvironmentsDialog } from "./AffectedEnvironmentsDialog"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Alert, AlertDescription } from "@/components/ui/alert"
import useCustomToast from "@/hooks/useCustomToast"

// Type display names and metadata
// NOTE: MiniMax is temporarily disabled in the UI (not currently supported).
const TYPE_OPTIONS: { value: AICredentialType; label: string; description: string }[] = [
  { value: "anthropic", label: "Anthropic", description: "Claude AI models (API Key or OAuth Token)" },
  { value: "openai", label: "OpenAI", description: "OpenAI API (GPT-4o, o3, etc.)" },
  { value: "openai_compatible", label: "OpenAI Compatible", description: "OpenAI-compatible endpoints (vLLM, custom)" },
  { value: "google", label: "Google", description: "Google AI (Gemini models via AI Studio)" },
]

// Default credential name suggested for a freshly-created credential, derived
// from the selected provider (e.g. "Anthropic Key").
function defaultCredentialName(type: AICredentialType): string {
  const label = TYPE_OPTIONS.find((o) => o.value === type)?.label ?? type
  return `${label} Key`
}

interface AICredentialDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  credential?: AICredentialPublic | null // If provided, we're editing
}

// Human-readable copy for the test-connection skip reasons. A skip means the
// connection is valid but model listing isn't applicable for this credential.
const TEST_SKIP_MESSAGES: Record<string, string> = {
  oauth_token_unsupported:
    "Connection valid — model listing isn't supported for OAuth tokens.",
  no_list_endpoint:
    "Connection valid — this provider doesn't expose a model list.",
  no_base_url: "Enter a Base URL to list available models.",
  unsupported_type: "Connection valid — model listing not supported.",
}

function describeTestResult(result: AICredentialTestResult): string {
  if (result.success) {
    if (result.skip_reason && TEST_SKIP_MESSAGES[result.skip_reason]) {
      return TEST_SKIP_MESSAGES[result.skip_reason]
    }
    return `Connection successful — ${result.model_count} model${
      result.model_count === 1 ? "" : "s"
    } available.`
  }
  if (result.error === "invalid_key") {
    return "Connection failed — the provider rejected this key."
  }
  return "Connection failed."
}

export function AICredentialDialog({
  open,
  onOpenChange,
  credential,
}: AICredentialDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Form state
  const [name, setName] = useState("")
  const [type, setType] = useState<AICredentialType>("anthropic")
  const [apiKey, setApiKey] = useState("")
  const [baseUrl, setBaseUrl] = useState("")
  const [model, setModel] = useState("")
  const [setAsDefault, setSetAsDefault] = useState(false)
  const [expiryNotificationDate, setExpiryNotificationDate] = useState("")
  const [showInstructionsModal, setShowInstructionsModal] = useState(false)

  // State for affected environments dialog
  const [showAffectedDialog, setShowAffectedDialog] = useState(false)
  const [updatedCredentialId, setUpdatedCredentialId] = useState<string | null>(null)

  // Test Connection result (inline alert)
  const [testResult, setTestResult] = useState<AICredentialTestResult | null>(null)

  // Tracks the last auto-suggested name so we only overwrite it while the user
  // hasn't typed their own name.
  const autoNameRef = useRef("")

  const isEditing = !!credential

  // Reset form when dialog opens/closes or credential changes
  useEffect(() => {
    if (open) {
      setTestResult(null)
      if (credential) {
        // Editing existing credential
        setName(credential.name)
        setType(credential.type)
        setApiKey("") // Don't show existing key for security
        setBaseUrl(credential.base_url || "")
        setModel(credential.model || "")
        setSetAsDefault(credential.is_default)
        // Format date for input (YYYY-MM-DD)
        setExpiryNotificationDate(
          credential.expiry_notification_date
            ? new Date(credential.expiry_notification_date).toISOString().split('T')[0]
            : ""
        )
      } else {
        // Creating new credential — prefill the name with the provider default.
        const suggested = defaultCredentialName("anthropic")
        autoNameRef.current = suggested
        setName(suggested)
        setType("anthropic")
        setApiKey("")
        setBaseUrl("")
        setModel("")
        setSetAsDefault(false)
        setExpiryNotificationDate("")
      }
    }
  }, [open, credential])

  // Auto-set expiry date when OAuth token is entered
  useEffect(() => {
    if (type === "anthropic" && apiKey && apiKey.startsWith("sk-ant-oat")) {
      // Detected OAuth token - auto-set expiry to 11 months from now
      const elevenMonthsFromNow = new Date()
      elevenMonthsFromNow.setDate(elevenMonthsFromNow.getDate() + 335) // ~11 months
      const formattedDate = elevenMonthsFromNow.toISOString().split('T')[0]
      setExpiryNotificationDate(formattedDate)
    }
  }, [apiKey, type])

  // Create mutation
  const createMutation = useMutation({
    mutationFn: async () => {
      const result = await AiCredentialsService.createAiCredential({
        requestBody: {
          name,
          type,
          api_key: apiKey,
          base_url: (type === "openai_compatible" || type === "google") ? (baseUrl || undefined) : undefined,
          model: type === "openai_compatible" ? (model || undefined) : undefined,
          expiry_notification_date: expiryNotificationDate ? new Date(expiryNotificationDate).toISOString() : undefined,
        },
      })
      // If set as default, make another call to set it as default
      if (setAsDefault) {
        await AiCredentialsService.setAiCredentialDefault({
          credentialId: result.id,
        })
      }
      return result
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsList"] })
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      showSuccessToast("AI credential created successfully")
      onOpenChange(false)
    },
    onError: (error: Error) => {
      showErrorToast(error.message || "Failed to create AI credential")
    },
  })

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: async () => {
      // Build update payload - only include changed fields
      const updatePayload: Record<string, string | null | undefined> = {}
      if (name !== credential?.name) {
        updatePayload.name = name
      }
      if (apiKey) {
        // Only update API key if provided (not empty)
        updatePayload.api_key = apiKey
      }
      if (type === "openai_compatible" || type === "google") {
        if (baseUrl !== credential?.base_url) {
          updatePayload.base_url = baseUrl || null
        }
      }
      if (type === "openai_compatible") {
        if (model !== credential?.model) {
          updatePayload.model = model || null
        }
      }

      // Check if expiry_notification_date changed
      const existingDate = credential?.expiry_notification_date
        ? new Date(credential.expiry_notification_date).toISOString().split('T')[0]
        : ""
      if (expiryNotificationDate !== existingDate) {
        updatePayload.expiry_notification_date = expiryNotificationDate
          ? new Date(expiryNotificationDate).toISOString()
          : null
      }

      // Only call update if there are changes
      if (Object.keys(updatePayload).length > 0) {
        await AiCredentialsService.updateAiCredential({
          credentialId: credential!.id,
          requestBody: updatePayload,
        })
      }

      // Handle default status change
      if (setAsDefault && !credential?.is_default) {
        await AiCredentialsService.setAiCredentialDefault({
          credentialId: credential!.id,
        })
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsList"] })
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      showSuccessToast("AI credential updated successfully")

      // Trigger affected environments dialog
      if (credential?.id) {
        setUpdatedCredentialId(credential.id)
        setShowAffectedDialog(true)
      }

      onOpenChange(false)
    },
    onError: (error: Error) => {
      showErrorToast(error.message || "Failed to update AI credential")
    },
  })

  // Test Connection mutation — validates the key and (in Edit) force-refreshes
  // the credential's discovered model list.
  const testMutation = useMutation({
    mutationFn: () =>
      AiCredentialsService.testAiCredentialConnection({
        requestBody: {
          type,
          api_key: apiKey || undefined,
          base_url:
            type === "openai_compatible" || type === "google"
              ? baseUrl || undefined
              : undefined,
          credential_id: isEditing ? credential!.id : undefined,
        },
      }),
    onSuccess: (result) => {
      setTestResult(result)
      // A successful Edit test persists fresh discovered_models — propagate to
      // the list query that backs the model-override datalist suggestions.
      if (result.success && isEditing) {
        queryClient.invalidateQueries({ queryKey: ["aiCredentialsList"] })
      }
    },
    onError: (err: Error) => {
      setTestResult({
        success: false,
        models: [],
        model_count: 0,
        error: err.message || "Connection failed",
      })
    },
  })

  const handleSubmit = () => {
    if (isEditing) {
      updateMutation.mutate()
    } else {
      createMutation.mutate()
    }
  }

  const isPending = createMutation.isPending || updateMutation.isPending
  const error = createMutation.error || updateMutation.error

  // Test Connection is testable when there's a key to test:
  //  - Add: requires apiKey (and base_url + model for openai_compatible).
  //  - Edit: always allowed (falls back to the stored key when apiKey blank).
  const canTest = (): boolean => {
    if (testMutation.isPending) return false
    if (isEditing) return true
    if (apiKey.trim() === "") return false
    if (type === "openai_compatible")
      return baseUrl.trim() !== "" && model.trim() !== ""
    return true
  }

  // Validation — different required fields per type
  const isValid = (): boolean => {
    if (name.trim() === "") return false
    if (!isEditing && apiKey.trim() === "") return false
    if (type === "openai_compatible") return baseUrl.trim() !== "" && model.trim() !== ""
    return true
  }

  return (
    <>
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle>{isEditing ? "Edit AI Credential" : "Add AI Credential"}</DialogTitle>
          <DialogDescription>
            {isEditing
              ? "Update your AI credential settings. Leave API key blank to keep existing."
              : "Create a new named AI credential for your agents."}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          {/* Name */}
          <div className="space-y-2">
            <Label htmlFor="credential-name">Name</Label>
            <Input
              id="credential-name"
              placeholder="e.g., Production Anthropic"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>

          {/* Type */}
          <div className="space-y-2">
            <Label htmlFor="credential-type">Type</Label>
            <Select
              value={type}
              onValueChange={(v) => {
                const next = v as AICredentialType
                setType(next)
                // Keep the suggested name in sync with the provider until the
                // user types their own name.
                if (name === "" || name === autoNameRef.current) {
                  const suggested = defaultCredentialName(next)
                  autoNameRef.current = suggested
                  setName(suggested)
                }
              }}
              disabled={isEditing} // Can't change type when editing
            >
              <SelectTrigger id="credential-type">
                <SelectValue placeholder="Select credential type" />
              </SelectTrigger>
              <SelectContent>
                {TYPE_OPTIONS.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    <div>
                      <div>{opt.label}</div>
                      <div className="text-xs text-muted-foreground">{opt.description}</div>
                    </div>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* Anthropic Instructions Banner */}
          {type === "anthropic" && (
            <div className="flex items-center gap-2 p-3 rounded-lg border border-violet-200 dark:border-violet-800 bg-violet-50 dark:bg-violet-950/30">
              <Info className="h-4 w-4 text-violet-600 dark:text-violet-400 flex-shrink-0" />
              <p className="text-sm text-violet-700 dark:text-violet-300 flex-1">
                Anthropic supports API Keys (sk-ant-api...) and OAuth Tokens (sk-ant-oat...)
              </p>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setShowInstructionsModal(true)}
                className="text-violet-600 hover:text-violet-700 dark:text-violet-400"
              >
                Instructions
              </Button>
            </div>
          )}

          {/* API Key */}
          <div className="space-y-2">
            <Label htmlFor="credential-api-key">
              API Key{" "}
              {isEditing && <span className="text-muted-foreground">(leave blank to keep existing)</span>}
            </Label>
            <Input
              id="credential-api-key"
              type="password"
              placeholder={isEditing ? "••••••••••••••••" : "Enter API key"}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </div>

          {/* Expiry Notification Date */}
          <div className="space-y-2">
            <Label htmlFor="credential-expiry">
              Expiry Notification Date (Optional)
            </Label>
            <Input
              id="credential-expiry"
              type="date"
              value={expiryNotificationDate}
              onChange={(e) => setExpiryNotificationDate(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              {type === "anthropic"
                ? "Auto-fills to 11 months from now when you enter an OAuth token (sk-ant-oat...). You can adjust this date."
                : "Set a date to receive a reminder before this credential expires"}
            </p>
          </div>

          {/* OpenAI Compatible specific fields */}
          {type === "openai_compatible" && (
            <>
              <div className="space-y-2">
                <Label htmlFor="credential-base-url">Base URL</Label>
                <Input
                  id="credential-base-url"
                  type="text"
                  placeholder="https://api.example.com/v1"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  OpenAI-compatible API endpoint URL
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="credential-model">Model</Label>
                <Input
                  id="credential-model"
                  type="text"
                  placeholder="llama3.2:latest"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  Default model identifier (e.g., llama3.2:latest, gpt-4)
                </p>
              </div>
            </>
          )}

          {/* Google AI specific fields */}
          {type === "google" && (
            <div className="space-y-2">
              <Label htmlFor="credential-base-url-google">
                API Endpoint{" "}
                <span className="text-muted-foreground">(optional)</span>
              </Label>
              <Input
                id="credential-base-url-google"
                type="text"
                placeholder="https://generativelanguage.googleapis.com"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Custom Google AI endpoint URL. Leave blank for the default.
              </p>
            </div>
          )}

          {/* Set as default checkbox */}
          <div className="flex items-center space-x-2">
            <Checkbox
              id="credential-default"
              checked={setAsDefault}
              onCheckedChange={(checked) => setSetAsDefault(checked === true)}
              disabled={credential?.is_default} // Already default, can't unset here
            />
            <Label htmlFor="credential-default" className="text-sm font-normal">
              Set as default for {TYPE_OPTIONS.find(t => t.value === type)?.label || type}
              {credential?.is_default && " (already default)"}
            </Label>
          </div>

          {testResult && (
            <Alert variant={testResult.success ? "default" : "destructive"}>
              {testResult.success && (
                <CheckCircle2 className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
              )}
              <AlertDescription>{describeTestResult(testResult)}</AlertDescription>
            </Alert>
          )}

          {error && (
            <Alert variant="destructive">
              <AlertDescription>
                {(error as Error).message || "An error occurred"}
              </AlertDescription>
            </Alert>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="secondary"
            onClick={() => testMutation.mutate()}
            disabled={!canTest() || isPending}
          >
            {testMutation.isPending ? (
              <>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                Testing...
              </>
            ) : (
              "Test Connection"
            )}
          </Button>
          <Button onClick={handleSubmit} disabled={isPending || testMutation.isPending || !isValid()}>
            {isPending ? (
              <>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                {isEditing ? "Updating..." : "Creating..."}
              </>
            ) : (
              isEditing ? "Update" : "Create"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    {/* Anthropic Instructions Modal */}
    <AnthropicCredentialsModal
      open={showInstructionsModal}
      onOpenChange={setShowInstructionsModal}
    />

    {/* Affected Environments Dialog */}
    {updatedCredentialId && (
      <AffectedEnvironmentsDialog
        open={showAffectedDialog}
        onOpenChange={setShowAffectedDialog}
        credentialId={updatedCredentialId}
        credentialName={credential?.name || ""}
      />
    )}
    </>
  )
}
