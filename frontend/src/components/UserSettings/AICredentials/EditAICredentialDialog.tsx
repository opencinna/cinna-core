import { useMutation, useQueryClient } from "@tanstack/react-query"
import { CheckCircle2, Loader2 } from "lucide-react"
import { useEffect, useState } from "react"

import type { AICredentialPublic, AICredentialTestResult } from "@/client"
import { AiCredentialsService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { describeTestResult, getTypeDisplayName } from "./credentialTypes"

interface EditAICredentialDialogProps {
  credential: AICredentialPublic
  open: boolean
  onOpenChange: (open: boolean) => void
  /**
   * Called once the update has landed and this dialog has closed, so the row
   * can chain the affected-environments dialog. Chained, never nested.
   */
  onSaved: () => void
}

/** `expiry_notification_date` as the `<input type="date">` value. */
function toDateInput(value: string | null | undefined): string {
  if (!value) return ""
  return new Date(value).toISOString().split("T")[0]
}

/**
 * Edit one AI credential — P2's edit-dialog leg, one section.
 *
 * Split from the Create story (`AddAICredentialWizard`) because the type is
 * immutable after creation: the create form needs a type step this one must
 * not show, and a disabled `<Select>` is a control that cannot be used. The
 * long-form Anthropic guide that used to open on top of this dialog now lives
 * behind the card header's `⋯`.
 */
export function EditAICredentialDialog({
  credential,
  open,
  onOpenChange,
  onSaved,
}: EditAICredentialDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [name, setName] = useState(credential.name)
  const [apiKey, setApiKey] = useState("")
  const [baseUrl, setBaseUrl] = useState(credential.base_url || "")
  const [model, setModel] = useState(credential.model || "")
  const [setAsDefault, setSetAsDefault] = useState(credential.is_default)
  const [expiryDate, setExpiryDate] = useState(
    toDateInput(credential.expiry_notification_date),
  )
  const [testResult, setTestResult] = useState<AICredentialTestResult | null>(
    null,
  )

  // Re-seed when the dialog is (re-)opened for this credential.
  useEffect(() => {
    if (!open) return
    setName(credential.name)
    setApiKey("")
    setBaseUrl(credential.base_url || "")
    setModel(credential.model || "")
    setSetAsDefault(credential.is_default)
    setExpiryDate(toDateInput(credential.expiry_notification_date))
    setTestResult(null)
  }, [open, credential])

  // Swapping an API key for an OAuth token re-dates the reminder: OAuth tokens
  // expire in a year and the platform suggests eleven months (335 days).
  useEffect(() => {
    if (credential.type === "anthropic" && apiKey.startsWith("sk-ant-oat")) {
      const target = new Date()
      target.setDate(target.getDate() + 335)
      setExpiryDate(target.toISOString().split("T")[0])
    }
  }, [apiKey, credential.type])

  const updateMutation = useMutation({
    mutationFn: async () => {
      // Only changed fields travel; a blank API key means "keep the stored one".
      const payload: Record<string, string | null | undefined> = {}
      if (name !== credential.name) payload.name = name
      if (apiKey) payload.api_key = apiKey
      if (
        credential.type === "openai_compatible" ||
        credential.type === "google"
      ) {
        if (baseUrl !== (credential.base_url || "")) {
          payload.base_url = baseUrl || null
        }
      }
      if (credential.type === "openai_compatible") {
        if (model !== (credential.model || "")) payload.model = model || null
      }
      if (expiryDate !== toDateInput(credential.expiry_notification_date)) {
        payload.expiry_notification_date = expiryDate
          ? new Date(expiryDate).toISOString()
          : null
      }

      if (Object.keys(payload).length > 0) {
        await AiCredentialsService.updateAiCredential({
          credentialId: credential.id,
          requestBody: payload,
        })
      }

      if (setAsDefault && !credential.is_default) {
        await AiCredentialsService.setAiCredentialDefault({
          credentialId: credential.id,
        })
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsList"] })
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      queryClient.invalidateQueries({ queryKey: ["resolveDefaultCredential"] })
      showSuccessToast("AI credential updated")
      onOpenChange(false)
      onSaved()
    },
    onError: (error: Error) =>
      showErrorToast(error.message || "Failed to update AI credential"),
  })

  // A successful test on a saved credential persists a fresh discovered-model
  // list, so the list query that feeds the model datalists is invalidated too.
  const testMutation = useMutation({
    mutationFn: () =>
      AiCredentialsService.testAiCredentialConnection({
        requestBody: {
          type: credential.type,
          api_key: apiKey || undefined,
          base_url:
            credential.type === "openai_compatible" ||
            credential.type === "google"
              ? baseUrl || undefined
              : undefined,
          credential_id: credential.id,
        },
      }),
    onSuccess: (result) => {
      setTestResult(result)
      if (result.success) {
        queryClient.invalidateQueries({ queryKey: ["aiCredentialsList"] })
      }
    },
    onError: (err: Error) =>
      setTestResult({
        success: false,
        models: [],
        model_count: 0,
        error: err.message || "Connection failed",
      }),
  })

  const isSaving = updateMutation.isPending
  const isValid =
    name.trim() !== "" &&
    (credential.type !== "openai_compatible" ||
      (baseUrl.trim() !== "" && model.trim() !== ""))

  return (
    <Dialog
      open={open}
      // Escape and outside-click must not close the dialog mid-request: the
      // pending state lives on the Update button (guidelines §6).
      onOpenChange={(next) => {
        if (!isSaving) onOpenChange(next)
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Edit AI credential</DialogTitle>
          <DialogDescription>
            {getTypeDisplayName(credential.type)} — the provider can&apos;t be
            changed after creation.
          </DialogDescription>
        </DialogHeader>

        <fieldset className="grid gap-4 py-2" disabled={isSaving}>
          <div className="space-y-2">
            <Label htmlFor="edit-credential-name">Name</Label>
            <Input
              id="edit-credential-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="edit-credential-key">
              API key{" "}
              <span className="text-muted-foreground">
                (leave blank to keep the current one)
              </span>
            </Label>
            <Input
              id="edit-credential-key"
              type="password"
              placeholder="••••••••••••••••"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </div>

          {credential.type === "openai_compatible" && (
            <>
              <div className="space-y-2">
                <Label htmlFor="edit-credential-base-url">Base URL</Label>
                <Input
                  id="edit-credential-base-url"
                  placeholder="https://api.example.com/v1"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="edit-credential-model">Model</Label>
                <Input
                  id="edit-credential-model"
                  placeholder="llama3.2:latest"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                />
              </div>
            </>
          )}

          {credential.type === "google" && (
            <div className="space-y-2">
              <Label htmlFor="edit-credential-endpoint">
                API endpoint{" "}
                <span className="text-muted-foreground">(optional)</span>
              </Label>
              <Input
                id="edit-credential-endpoint"
                placeholder="https://generativelanguage.googleapis.com"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
              />
            </div>
          )}

          <div className="space-y-2">
            <Label htmlFor="edit-credential-expiry">
              Expiry notification date{" "}
              <span className="text-muted-foreground">(optional)</span>
            </Label>
            <Input
              id="edit-credential-expiry"
              type="date"
              value={expiryDate}
              onChange={(e) => setExpiryDate(e.target.value)}
            />
          </div>

          <div className="flex items-center space-x-2">
            <Checkbox
              id="edit-credential-default"
              checked={setAsDefault}
              onCheckedChange={(checked) => setSetAsDefault(checked === true)}
              disabled={credential.is_default}
            />
            <Label
              htmlFor="edit-credential-default"
              className="text-sm font-normal"
            >
              Set as default for {getTypeDisplayName(credential.type)}
              {credential.is_default && " (already default)"}
            </Label>
          </div>

          {testResult && (
            <Alert variant={testResult.success ? "default" : "destructive"}>
              {testResult.success && (
                <CheckCircle2 className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
              )}
              <AlertDescription>
                {describeTestResult(testResult)}
              </AlertDescription>
            </Alert>
          )}

          {updateMutation.error && (
            <Alert variant="destructive">
              <AlertDescription>
                {(updateMutation.error as Error).message || "An error occurred"}
              </AlertDescription>
            </Alert>
          )}
        </fieldset>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={isSaving}
          >
            Cancel
          </Button>
          <Button
            variant="secondary"
            onClick={() => testMutation.mutate()}
            disabled={isSaving || testMutation.isPending}
          >
            {testMutation.isPending ? (
              <>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                Testing…
              </>
            ) : (
              "Test connection"
            )}
          </Button>
          <LoadingButton
            loading={isSaving}
            disabled={!isValid || testMutation.isPending}
            onClick={() => updateMutation.mutate()}
          >
            Update
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
