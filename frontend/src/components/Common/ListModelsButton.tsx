import { useState } from "react"
import { useMutation } from "@tanstack/react-query"
import { ListChecks, Search, Loader2, AlertCircle, Info } from "lucide-react"
import { AiCredentialsService } from "@/client"
import type { AICredentialType, AICredentialTestResult } from "@/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"

interface ListModelsButtonProps {
  /** Stored credential id whose live model list should be fetched. */
  credentialId: string | null
  /** Credential type, required by the test-connection request body. */
  credentialType: AICredentialType | null
  /**
   * Called with the bare model id (exactly as returned by the provider /
   * `discovered_models`) when the user picks a model. The caller writes this
   * into its Model Override field.
   */
  onSelect: (modelId: string) => void
  /** Optional external disable (e.g. while the surrounding form is saving). */
  disabled?: boolean
}

// Human-readable copy for the skip reasons the backend may return alongside
// `success=true` (model listing valid but unsupported for this credential).
const SKIP_REASON_MESSAGES: Record<string, string> = {
  oauth_token_unsupported:
    "This credential uses an OAuth token, which can't list models. You can still type a model id manually.",
  no_list_endpoint:
    "This provider doesn't expose a model-listing endpoint. You can still type a model id manually.",
  no_base_url:
    "No base URL is configured for this credential, so models can't be listed. You can still type a model id manually.",
}

function describeSkipReason(reason: string | null | undefined): string {
  if (reason && SKIP_REASON_MESSAGES[reason]) return SKIP_REASON_MESSAGES[reason]
  return "Model listing isn't supported for this credential. You can still type a model id manually."
}

function describeError(error: string | null | undefined): string {
  if (error === "invalid_key") {
    return "The provider rejected this credential's key. Check the credential and try again."
  }
  return "Couldn't list models for this credential. Please try again."
}

interface ProbeArgs {
  credentialId: string
  credentialType: AICredentialType
}

export function ListModelsButton({
  credentialId,
  credentialType,
  onSelect,
  disabled,
}: ListModelsButtonProps) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState("")

  const mutation = useMutation<AICredentialTestResult, Error, ProbeArgs>({
    mutationFn: (args) =>
      AiCredentialsService.testAiCredentialConnection({
        requestBody: {
          // `type` is required by the request schema; the backend resolves the
          // actual key from the stored credential via `credential_id`.
          type: args.credentialType,
          credential_id: args.credentialId,
        },
      }),
  })

  const noCredential = !credentialId || !credentialType
  const isDisabled = disabled || noCredential

  // Bind the probed credential into the mutation variables so the rendered
  // result (and Retry) is unambiguously tied to what was requested.
  const probe = () => {
    if (!credentialId || !credentialType) return
    mutation.mutate({ credentialId, credentialType })
  }

  const handleOpen = () => {
    setFilter("")
    // Clear the previous credential's result/error before re-probing so the
    // dialog shows the loading state immediately rather than flashing stale
    // data when re-opened after the selected credential changed.
    mutation.reset()
    setOpen(true)
    probe()
  }

  const handlePick = (modelId: string) => {
    onSelect(modelId)
    setOpen(false)
  }

  const result = mutation.data
  const models = result?.models ?? []
  const trimmedFilter = filter.trim().toLowerCase()
  const filteredModels = trimmedFilter
    ? models.filter((m) => m.toLowerCase().includes(trimmedFilter))
    : models

  const renderBody = () => {
    if (mutation.isPending) {
      return (
        <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Fetching models…
        </div>
      )
    }

    if (mutation.isError) {
      return (
        <div className="flex flex-col items-center gap-2 py-10 text-center text-sm text-muted-foreground">
          <AlertCircle className="h-5 w-5 text-destructive" />
          <p>Couldn't list models for this credential. Please try again.</p>
          <Button variant="outline" size="sm" onClick={probe}>
            Retry
          </Button>
        </div>
      )
    }

    if (!result) return null

    // Hard failure (e.g. invalid_key).
    if (!result.success) {
      return (
        <div className="flex flex-col items-center gap-2 py-10 text-center text-sm text-muted-foreground">
          <AlertCircle className="h-5 w-5 text-destructive" />
          <p>{describeError(result.error)}</p>
          <Button variant="outline" size="sm" onClick={probe}>
            Retry
          </Button>
        </div>
      )
    }

    // Valid connection, but listing unsupported for this credential type/token.
    // Per the contract, `error` and `skip_reason` are mutually exclusive and
    // `error` only appears with `success=false` (handled above), so checking
    // `skip_reason` alone is sufficient.
    if (result.skip_reason) {
      return (
        <div className="flex flex-col items-center gap-2 py-10 text-center text-sm text-muted-foreground">
          <Info className="h-5 w-5 text-muted-foreground" />
          <p>{describeSkipReason(result.skip_reason)}</p>
        </div>
      )
    }

    if (models.length === 0) {
      return (
        <div className="py-10 text-center text-sm text-muted-foreground">
          No models returned for this credential.
        </div>
      )
    }

    return (
      <>
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter models…"
            className="h-9 pl-8"
            autoFocus
          />
        </div>
        <div className="mt-3 max-h-72 space-y-1 overflow-y-auto pr-1">
          {filteredModels.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              No models match "{filter}".
            </p>
          ) : (
            filteredModels.map((model) => (
              <button
                key={model}
                type="button"
                onClick={() => handlePick(model)}
                className="w-full rounded-md border px-3 py-2 text-left text-sm transition-colors hover:border-primary hover:bg-accent"
              >
                <span className="font-mono">{model}</span>
              </button>
            ))
          )}
        </div>
      </>
    )
  }

  return (
    <>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            {/* Span wrapper so the tooltip still works while the button is disabled. */}
            <span className="inline-flex">
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-9 shrink-0"
                onClick={handleOpen}
                disabled={isDisabled}
              >
                <ListChecks className="h-3.5 w-3.5 mr-1.5" />
                List models
              </Button>
            </span>
          </TooltipTrigger>
          <TooltipContent side="top" className="text-xs">
            {noCredential
              ? "Select a credential first"
              : "Fetch this credential's live model list"}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-[440px]">
          <DialogHeader>
            <DialogTitle>Available models</DialogTitle>
            <DialogDescription>
              Pick a model to use as the override for this mode.
            </DialogDescription>
          </DialogHeader>
          {renderBody()}
        </DialogContent>
      </Dialog>
    </>
  )
}
