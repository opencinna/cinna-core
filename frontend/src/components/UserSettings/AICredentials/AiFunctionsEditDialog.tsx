import { AlertCircle, Sparkles } from "lucide-react"
import { useEffect, useState } from "react"

import type { AICredentialPublic } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

/** The stored `default_ai_functions_credential_id` is null for "use default". */
const USE_DEFAULT = "default"

interface AiFunctionsEditDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** `default_ai_functions_sdk`: "system" | "personal:anthropic" | "personal:openai". */
  provider: string
  credentialId: string | null
  credentials: AICredentialPublic[]
  onSave: (provider: string, credentialId: string | null) => void
  isSaving: boolean
}

/**
 * Which provider and key the platform uses for its own AI calls — titles,
 * schedules, suggestions.
 *
 * P2's edit-dialog leg, the same shape as `SDKModeEditDialog`, which is what
 * makes the third row of the SDK card behave like its two siblings. It is a
 * dialog rather than an auto-saving `Select` on the card for two reasons that
 * each settle it on their own: one interaction model per card, and a setting
 * with two fields is not an inline form (guidelines §2).
 */
export function AiFunctionsEditDialog({
  open,
  onOpenChange,
  provider: initialProvider,
  credentialId: initialCredentialId,
  credentials,
  onSave,
  isSaving,
}: AiFunctionsEditDialogProps) {
  const [provider, setProvider] = useState(initialProvider)
  const [credentialId, setCredentialId] = useState(
    initialCredentialId ?? USE_DEFAULT,
  )

  useEffect(() => {
    if (!open) return
    setProvider(initialProvider)
    // A stored id the Select cannot offer — the credential was deleted, or it
    // belongs to the other provider's type — opens the field on "Use Default"
    // rather than on an empty Select that would save the dead id back.
    const type = initialProvider === "personal:openai" ? "openai" : "anthropic"
    const selectable =
      !!initialCredentialId &&
      credentials.some((c) => c.id === initialCredentialId && c.type === type)
    setCredentialId(selectable ? initialCredentialId : USE_DEFAULT)
  }, [open, initialProvider, initialCredentialId, credentials])

  const isPersonal = provider.startsWith("personal:")
  const providerType = provider === "personal:openai" ? "openai" : "anthropic"
  const providerLabel = providerType === "openai" ? "OpenAI" : "Anthropic"

  const eligible = credentials.filter((c) => c.type === providerType)
  const selected = eligible.find((c) => c.id === credentialId) ?? null
  const resolvedDefault = eligible.find((c) => c.is_default) ?? null

  // OAuth tokens cannot talk to the Anthropic Messages API the AI functions
  // use, so the selection — or the default it would resolve to — blocks Save.
  const oauthBlocked =
    isPersonal &&
    providerType === "anthropic" &&
    (credentialId === USE_DEFAULT
      ? !!resolvedDefault?.is_oauth_token
      : !!selected?.is_oauth_token)

  const handleProviderChange = (next: string) => {
    setProvider(next)
    // A credential of the old provider's type means nothing to the new one.
    setCredentialId(USE_DEFAULT)
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!isSaving) onOpenChange(next)
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-purple-500" />
            AI Functions
          </DialogTitle>
          <DialogDescription>
            The provider the platform uses for its own AI calls — titles,
            schedules and suggestions.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="space-y-1.5">
            <Label htmlFor="ai-functions-provider" className="text-sm">
              Provider
            </Label>
            <Select
              value={provider}
              onValueChange={handleProviderChange}
              disabled={isSaving}
            >
              <SelectTrigger id="ai-functions-provider" className="h-9">
                <SelectValue placeholder="Select provider" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="system">System (default)</SelectItem>
                <SelectItem value="personal:anthropic">
                  Personal Anthropic
                </SelectItem>
                <SelectItem value="personal:openai">Personal OpenAI</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {isPersonal &&
            (eligible.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No {providerLabel} credentials yet. Add one from the AI
                Credentials card.
              </p>
            ) : (
              <div className="space-y-1.5">
                <Label htmlFor="ai-functions-credential" className="text-sm">
                  Credential
                </Label>
                <Select
                  value={credentialId}
                  onValueChange={setCredentialId}
                  disabled={isSaving}
                >
                  <SelectTrigger id="ai-functions-credential" className="h-9">
                    <SelectValue placeholder="Select credential" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={USE_DEFAULT}>
                      Use Default
                      {resolvedDefault ? ` (${resolvedDefault.name})` : ""}
                    </SelectItem>
                    {eligible.map((cred) => (
                      <SelectItem
                        key={cred.id}
                        value={cred.id}
                        disabled={cred.is_oauth_token}
                      >
                        {cred.name}
                        {cred.is_oauth_token && " (OAuth — incompatible)"}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            ))}

          {oauthBlocked && (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription className="text-xs">
                OAuth tokens can&apos;t be used with the Anthropic API. Pick a
                credential with an API key (sk-ant-api…).
              </AlertDescription>
            </Alert>
          )}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={isSaving}
          >
            Cancel
          </Button>
          <LoadingButton
            loading={isSaving}
            disabled={oauthBlocked}
            onClick={() =>
              onSave(
                provider,
                !isPersonal || credentialId === USE_DEFAULT
                  ? null
                  : credentialId,
              )
            }
          >
            Save
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
