import { useMutation, useQueryClient } from "@tanstack/react-query"
import { CheckCircle2, Info, Loader2 } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import type { AICredentialTestResult, AICredentialType } from "@/client"
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
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import {
  defaultCredentialName,
  describeTestResult,
  getTypeDisplayName,
  TYPE_OPTIONS,
} from "./credentialTypes"

interface AddAICredentialWizardProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * Add an AI credential — P6, two steps, step 1 borrowing P4's pill picker.
 *
 * The form's shape depends on the provider, which is what §1 Create means by
 * "choosing the type **is** step 1 — never a `<Select>` at the top of a long
 * form". That `<Select>` is what this replaces (anti-pattern A7).
 *
 * Nothing here opens a dialog. What a user needs while pasting an Anthropic
 * key is the inline alert on step 2; the 289-line setup guide that used to
 * open a second modal on top of this one now sits behind the card header's
 * `⋯` (anti-pattern A2).
 */
export function AddAICredentialWizard({
  open,
  onOpenChange,
}: AddAICredentialWizardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [step, setStep] = useState<"provider" | "details">("provider")
  const [type, setType] = useState<AICredentialType>("anthropic")
  const [name, setName] = useState("")
  const [apiKey, setApiKey] = useState("")
  const [baseUrl, setBaseUrl] = useState("")
  const [model, setModel] = useState("")
  const [expiryDate, setExpiryDate] = useState("")
  const [setAsDefault, setSetAsDefault] = useState(false)
  const [testResult, setTestResult] = useState<AICredentialTestResult | null>(
    null,
  )

  // The last name this dialog suggested; a name the user typed is never
  // overwritten when they go Back and pick a different provider.
  const autoNameRef = useRef("")

  // Pasting an OAuth token dates the reminder eleven months out (335 days),
  // which is what the platform suggests for a one-year token.
  useEffect(() => {
    if (type === "anthropic" && apiKey.startsWith("sk-ant-oat")) {
      const target = new Date()
      target.setDate(target.getDate() + 335)
      setExpiryDate(target.toISOString().split("T")[0])
    }
  }, [apiKey, type])

  const createMutation = useMutation({
    mutationFn: async () => {
      const created = await AiCredentialsService.createAiCredential({
        requestBody: {
          name,
          type,
          api_key: apiKey,
          base_url:
            type === "openai_compatible" || type === "google"
              ? baseUrl || undefined
              : undefined,
          model: type === "openai_compatible" ? model || undefined : undefined,
          expiry_notification_date: expiryDate
            ? new Date(expiryDate).toISOString()
            : undefined,
        },
      })
      if (setAsDefault) {
        await AiCredentialsService.setAiCredentialDefault({
          credentialId: created.id,
        })
      }
      return created
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsList"] })
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      queryClient.invalidateQueries({ queryKey: ["resolveDefaultCredential"] })
      showSuccessToast("AI credential created")
      // A brand-new credential is linked to no environment, so nothing is
      // chained here — the dialog just closes.
      onOpenChange(false)
    },
    onError: (error: Error) =>
      showErrorToast(error.message || "Failed to create AI credential"),
  })

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
        },
      }),
    onSuccess: setTestResult,
    onError: (err: Error) =>
      setTestResult({
        success: false,
        models: [],
        model_count: 0,
        error: err.message || "Connection failed",
      }),
  })

  const isSaving = createMutation.isPending

  const chooseProvider = (next: AICredentialType) => {
    setType(next)
    if (name === "" || name === autoNameRef.current) {
      const suggested = defaultCredentialName(next)
      autoNameRef.current = suggested
      setName(suggested)
    }
    setTestResult(null)
    setStep("details")
  }

  const needsEndpointAndModel = type === "openai_compatible"
  const isValid =
    name.trim() !== "" &&
    apiKey.trim() !== "" &&
    (!needsEndpointAndModel || (baseUrl.trim() !== "" && model.trim() !== ""))
  const canTest =
    apiKey.trim() !== "" &&
    (!needsEndpointAndModel || (baseUrl.trim() !== "" && model.trim() !== ""))

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!isSaving) onOpenChange(next)
      }}
    >
      <DialogContent className="sm:max-w-lg">
        {/* One <form> across both steps with hidden panes, so a value typed on
            step 2 survives a trip Back to the picker (the P6 reference does
            the same). */}
        <form
          onSubmit={(e) => {
            e.preventDefault()
            if (step === "details" && isValid) createMutation.mutate()
          }}
        >
          <DialogHeader>
            <p className="text-xs text-muted-foreground">
              <span
                className={
                  step === "provider"
                    ? "font-medium text-foreground"
                    : undefined
                }
              >
                1 Provider
              </span>
              {" · "}
              <span
                className={
                  step === "details" ? "font-medium text-foreground" : undefined
                }
              >
                2 Details
              </span>
            </p>
            <DialogTitle>Add AI credential</DialogTitle>
            <DialogDescription>
              {step === "provider"
                ? "Which provider is this key for?"
                : `Details for your ${getTypeDisplayName(type)} key.`}
            </DialogDescription>
          </DialogHeader>

          <div className="py-4" hidden={step !== "provider"}>
            <div className="flex flex-wrap gap-2">
              {TYPE_OPTIONS.map((option) => (
                <Tooltip key={option.value}>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      onClick={() => chooseProvider(option.value)}
                      className={cn(
                        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm font-medium transition-colors",
                        "hover:border-primary hover:bg-accent",
                      )}
                    >
                      {option.label}
                    </button>
                  </TooltipTrigger>
                  <TooltipContent side="bottom" className="text-xs">
                    {option.description}
                  </TooltipContent>
                </Tooltip>
              ))}
            </div>
          </div>

          <fieldset
            className="grid min-w-0 gap-4 py-4"
            hidden={step !== "details"}
            disabled={isSaving}
          >
            {type === "anthropic" && (
              <Alert>
                <Info className="h-4 w-4" />
                <AlertDescription className="text-xs">
                  Anthropic accepts an API key (
                  <code className="font-mono">sk-ant-api…</code>) from{" "}
                  <a
                    href="https://console.anthropic.com"
                    target="_blank"
                    rel="noreferrer"
                    className="text-primary underline underline-offset-2"
                  >
                    console.anthropic.com
                  </a>
                  , or an OAuth token (
                  <code className="font-mono">sk-ant-oat…</code>) from{" "}
                  <code className="font-mono">claude setup-token</code>. The
                  full guide is under the card&apos;s ⋯ menu.
                </AlertDescription>
              </Alert>
            )}

            <div className="space-y-2">
              <Label htmlFor="add-credential-name">Name</Label>
              <Input
                id="add-credential-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="add-credential-key">API key</Label>
              <Input
                id="add-credential-key"
                type="password"
                placeholder="Enter API key"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
              />
            </div>

            {needsEndpointAndModel && (
              <>
                <div className="space-y-2">
                  <Label htmlFor="add-credential-base-url">Base URL</Label>
                  <Input
                    id="add-credential-base-url"
                    placeholder="https://api.example.com/v1"
                    value={baseUrl}
                    onChange={(e) => setBaseUrl(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="add-credential-model">Model</Label>
                  <Input
                    id="add-credential-model"
                    placeholder="llama3.2:latest"
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                  />
                </div>
              </>
            )}

            {type === "google" && (
              <div className="space-y-2">
                <Label htmlFor="add-credential-endpoint">
                  API endpoint{" "}
                  <span className="text-muted-foreground">(optional)</span>
                </Label>
                <Input
                  id="add-credential-endpoint"
                  placeholder="https://generativelanguage.googleapis.com"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                />
              </div>
            )}

            <div className="space-y-2">
              <Label htmlFor="add-credential-expiry">
                Expiry notification date{" "}
                <span className="text-muted-foreground">(optional)</span>
              </Label>
              <Input
                id="add-credential-expiry"
                type="date"
                value={expiryDate}
                onChange={(e) => setExpiryDate(e.target.value)}
              />
            </div>

            <div className="flex items-center space-x-2">
              <Checkbox
                id="add-credential-default"
                checked={setAsDefault}
                onCheckedChange={(checked) => setSetAsDefault(checked === true)}
              />
              <Label
                htmlFor="add-credential-default"
                className="text-sm font-normal"
              >
                Set as default for {getTypeDisplayName(type)}
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

            {createMutation.error && (
              <Alert variant="destructive">
                <AlertDescription>
                  {(createMutation.error as Error).message ||
                    "An error occurred"}
                </AlertDescription>
              </Alert>
            )}
          </fieldset>

          <DialogFooter>
            {step === "provider" ? (
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
              >
                Cancel
              </Button>
            ) : (
              <>
                <Button
                  type="button"
                  variant="outline"
                  disabled={isSaving}
                  onClick={() => setStep("provider")}
                >
                  Back
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={!canTest || isSaving || testMutation.isPending}
                  onClick={() => testMutation.mutate()}
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
                  type="submit"
                  loading={isSaving}
                  disabled={!isValid || testMutation.isPending}
                >
                  Create
                </LoadingButton>
              </>
            )}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
