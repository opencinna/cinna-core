import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { AdminAiProvidersService, type AIProviderPublic } from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import {
  AI_PROVIDERS_QUERY_KEY,
  MANAGED_CREDENTIALS_QUERY_PREFIX,
} from "@/components/Admin/LlmProviders/providerTypes"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
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
import { handleError } from "@/utils"

interface ReplaceProviderKeyDialogProps {
  provider: AIProviderPublic
  typeLabel: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * Replace a `fixed_key` provider's key, and every member's copy of it.
 *
 * The dialog **is** the confirmation: §2 asks a destructive confirm to name the
 * entity and end in one button, and an `AlertDialog` cannot hold the input the
 * new key has to be typed into.
 *
 * Rendered only for `kind === "fixed_key"` — the row's menu omits the item for
 * a minted provider rather than disabling it, so the 400 the route answers
 * there is unreachable from the UI instead of caught after the fact.
 */
export function ReplaceProviderKeyDialog({
  provider,
  typeLabel,
  open,
  onOpenChange,
}: ReplaceProviderKeyDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  // No reset effect: `ProviderRow` mounts this only while it is open, so a key
  // typed and abandoned goes with the unmount.
  const [apiKey, setApiKey] = useState("")
  const [error, setError] = useState<string | null>(null)
  const memberCount = provider.member_count ?? 0

  const rotateMutation = useMutation({
    mutationFn: (key: string) =>
      AdminAiProvidersService.rotateAiProviderKey({
        providerId: provider.id,
        requestBody: { api_key: key },
      }),
    onSuccess: () => {
      showSuccessToast(
        `Key replaced for ${memberCount} ${memberCount === 1 ? "member" : "members"}.`,
      )
      onOpenChange(false)
    },
    onError: (err: ApiError) => handleError.call(showErrorToast, err),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: AI_PROVIDERS_QUERY_KEY })
      void queryClient.invalidateQueries({
        queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX,
      })
    },
  })

  const submit = () => {
    if (apiKey.trim() === "") {
      setError("A new API key is required")
      return
    }
    setError(null)
    rotateMutation.mutate(apiKey.trim())
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Replace the key on "{provider.name}"?</DialogTitle>
          <DialogDescription>
            Every member's copy is replaced immediately. {memberCount}{" "}
            {memberCount === 1 ? "person holds" : "people hold"} this key. The
            old key is not disabled at {typeLabel} — remove it there if you no
            longer want it live.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-2 py-2">
          <Label htmlFor={`rotate-${provider.id}`}>
            New API key <span className="text-destructive">*</span>
          </Label>
          <Input
            id={`rotate-${provider.id}`}
            type="password"
            autoComplete="off"
            placeholder="sk-…"
            value={apiKey}
            disabled={rotateMutation.isPending}
            onChange={(event) => setApiKey(event.target.value)}
          />
          {error && <p className="text-xs text-destructive">{error}</p>}
        </div>

        <DialogFooter>
          <DialogClose asChild>
            <Button
              type="button"
              variant="outline"
              disabled={rotateMutation.isPending}
            >
              Cancel
            </Button>
          </DialogClose>
          <LoadingButton
            type="button"
            loading={rotateMutation.isPending}
            onClick={submit}
          >
            Replace key
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
