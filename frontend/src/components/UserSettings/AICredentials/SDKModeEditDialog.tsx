import { useQuery } from "@tanstack/react-query"
import { MessageCircle, Save, Wrench } from "lucide-react"
import { useEffect, useState } from "react"

import type { AICredentialPublic } from "@/client"
import { AiCredentialsService } from "@/client"
import { ListModelsButton } from "@/components/Common/ListModelsButton"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  getCompatibleCredentials,
  SDK_ENGINE_OPTIONS,
  SUGGESTED_MODELS,
  TYPE_DISPLAY_NAMES,
  USE_DEFAULT_SENTINEL,
} from "./credentialTypes"

interface SDKModeEditDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  mode: "conversation" | "building"
  engine: string
  credentialId: string
  modelOverride: string
  credentials: AICredentialPublic[]
  onSave: (engine: string, credentialId: string, modelOverride: string) => void
  isSaving: boolean
}

/**
 * What one SDK mode runs on — engine, credential, model override.
 *
 * P2's edit-dialog leg, and the reference the pattern names. The model picker
 * beside the override field is a `Popover` anchored to that field, not a
 * second `Dialog`: a shared control that opens a dialog puts every form that
 * embeds it at disclosure depth 3 (guidelines §2).
 */
export function SDKModeEditDialog({
  open,
  onOpenChange,
  mode,
  engine: initialEngine,
  credentialId: initialCredentialId,
  modelOverride: initialModelOverride,
  credentials,
  onSave,
  isSaving,
}: SDKModeEditDialogProps) {
  const [engine, setEngine] = useState(initialEngine)
  const [credentialId, setCredentialId] = useState(initialCredentialId)
  const [modelOverride, setModelOverride] = useState(initialModelOverride)

  // Reset local state when the dialog opens.
  useEffect(() => {
    if (open) {
      setEngine(initialEngine)
      setCredentialId(initialCredentialId)
      setModelOverride(initialModelOverride)
    }
  }, [open, initialEngine, initialCredentialId, initialModelOverride])

  const compatible = getCompatibleCredentials(engine, credentials)
  const selectedCredential =
    credentials.find((c) => c.id === credentialId) ?? null
  // Prefer the admin-curated available_models when present, else the per-key
  // discovered models (see admin_curated_model_list).
  const discoveredModels = selectedCredential?.discovered_models ?? []
  const offeredModels = selectedCredential?.available_models?.length
    ? selectedCredential.available_models
    : discoveredModels
  const suggestedModels = selectedCredential
    ? Array.from(
        new Set([
          ...offeredModels,
          ...(SUGGESTED_MODELS[selectedCredential.type] ?? []),
        ]),
      )
    : []
  const overrideNotDiscovered =
    modelOverride.trim().length > 0 &&
    discoveredModels.length > 0 &&
    !discoveredModels.includes(modelOverride.trim())

  // Resolve the default credential for this engine.
  const { data: resolvedDefault } = useQuery({
    queryKey: ["resolveDefaultCredential", engine],
    queryFn: () =>
      AiCredentialsService.resolveDefaultCredential({ sdkEngine: engine }),
    enabled: open && credentialId === USE_DEFAULT_SENTINEL,
  })

  const handleEngineChange = (newEngine: string) => {
    setEngine(newEngine)
    setCredentialId(USE_DEFAULT_SENTINEL)
    setModelOverride("")
  }

  const isConversation = mode === "conversation"
  const datalistId = isConversation ? "conv-edit-models" : "build-edit-models"

  return (
    <Dialog
      open={open}
      // Escape and outside-click must not close the dialog mid-save.
      onOpenChange={(next) => {
        if (!isSaving) onOpenChange(next)
      }}
    >
      <DialogContent className="sm:max-w-[440px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {isConversation ? (
              <MessageCircle className="h-4 w-4 text-blue-500" />
            ) : (
              <Wrench className="h-4 w-4 text-orange-500" />
            )}
            {isConversation ? "Conversation Mode" : "Building Mode"}
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4 py-2">
          {/* SDK Engine */}
          <div className="space-y-1.5">
            <Label className="text-sm">SDK Engine</Label>
            <Select
              value={engine}
              onValueChange={handleEngineChange}
              disabled={isSaving}
            >
              <SelectTrigger className="h-9">
                <SelectValue placeholder="Select engine" />
              </SelectTrigger>
              <SelectContent>
                {SDK_ENGINE_OPTIONS.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    {opt.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* Credential */}
          <div className="space-y-1.5">
            <Label className="text-sm">Credential</Label>
            <Select
              value={credentialId}
              onValueChange={setCredentialId}
              disabled={isSaving}
            >
              <SelectTrigger className="h-9">
                <SelectValue placeholder="Select credential" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={USE_DEFAULT_SENTINEL}>
                  Use Default
                </SelectItem>
                {compatible.map((cred) => (
                  <SelectItem key={cred.id} value={cred.id}>
                    {cred.name}
                    {cred.is_default && " (default)"}
                    <span className="ml-1 text-xs text-muted-foreground">
                      ({cred.type})
                    </span>
                  </SelectItem>
                ))}
                {compatible.length === 0 && (
                  <div className="py-2 px-2 text-xs text-muted-foreground">
                    No compatible credentials
                  </div>
                )}
              </SelectContent>
            </Select>
            {credentialId === USE_DEFAULT_SENTINEL && (
              <p className="text-xs text-muted-foreground">
                {resolvedDefault
                  ? `Resolved: "${resolvedDefault.name}" (${
                      TYPE_DISPLAY_NAMES[resolvedDefault.type] ||
                      resolvedDefault.type
                    })`
                  : "No matching default credential"}
              </p>
            )}
          </div>

          {/* Model Override */}
          <div className="space-y-1.5">
            <Label className="text-sm">
              Model Override{" "}
              <span className="text-muted-foreground text-xs">(optional)</span>
            </Label>
            <div className="flex items-center gap-2">
              <Input
                list={datalistId}
                value={modelOverride}
                onChange={(e) => setModelOverride(e.target.value)}
                placeholder={
                  isConversation
                    ? "e.g., claude-haiku-4-5"
                    : "e.g., claude-opus-4"
                }
                className="h-9"
                disabled={isSaving}
              />
              <ListModelsButton
                credentialId={selectedCredential?.id ?? null}
                credentialType={selectedCredential?.type ?? null}
                onSelect={setModelOverride}
                disabled={isSaving}
              />
            </div>
            {suggestedModels.length > 0 && (
              <datalist id={datalistId}>
                {suggestedModels.map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
            )}
            {overrideNotDiscovered && (
              <p className="text-xs text-orange-600 dark:text-orange-400">
                This model isn&apos;t in the list of models this credential can
                access. Double-check the name.
              </p>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={isSaving}
          >
            Cancel
          </Button>
          {/* `LoadingButton`, like the AI Functions dialog opened from the row
              below this one: two dialogs reached from the same three-row card
              should not disagree about what a pending Save looks like. */}
          <LoadingButton
            loading={isSaving}
            onClick={() => onSave(engine, credentialId, modelOverride)}
          >
            <Save className="h-3.5 w-3.5 mr-2" />
            Save
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
