import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { AlertTriangle, Box, Loader2 } from "lucide-react"

import {
  type AICredentialDeletionImpact,
  type AICredentialPublic,
  AiCredentialsService,
} from "@/client"
import { ApiError } from "@/client/core/ApiError"
import {
  Alert,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"

interface DeleteAICredentialDialogProps {
  credential: AICredentialPublic | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

/** Extract an AICredentialDeletionImpact from a 409 ApiError body. */
function impactFromError(error: unknown): AICredentialDeletionImpact | null {
  if (error instanceof ApiError && error.status === 409) {
    const detail = (error.body as { detail?: unknown } | undefined)?.detail
    if (detail && typeof detail === "object" && "tier" in detail) {
      return detail as AICredentialDeletionImpact
    }
  }
  return null
}

export function DeleteAICredentialDialog({
  credential,
  open,
  onOpenChange,
}: DeleteAICredentialDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: impact, isLoading: impactLoading } = useQuery({
    queryKey: ["ai-credential-deletion-impact", credential?.id],
    queryFn: () =>
      AiCredentialsService.getAiCredentialDeletionImpact({
        credentialId: credential!.id,
      }),
    enabled: open && !!credential,
  })

  const deleteMutation = useMutation({
    mutationFn: (force: boolean) =>
      AiCredentialsService.deleteAiCredential({
        credentialId: credential!.id,
        force,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsList"] })
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      showSuccessToast("AI credential deleted successfully")
      onOpenChange(false)
    },
    onError: (error) => {
      if (impactFromError(error)) {
        queryClient.invalidateQueries({
          queryKey: ["ai-credential-deletion-impact", credential?.id],
        })
        showErrorToast(
          "This credential is now used by a published bundle. Review the impact below.",
        )
        return
      }
      showErrorToast("Failed to delete AI credential")
    },
  })

  const isTier2 = (impact?.tier ?? 0) === 2
  const bundleUsages = impact?.bundle_usages ?? []

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Delete AI Credential</DialogTitle>
          <DialogDescription>
            {credential?.name
              ? `"${credential.name}" will be permanently deleted.`
              : "This AI credential will be permanently deleted."}{" "}
            You will not be able to undo this action.
          </DialogDescription>
        </DialogHeader>

        {impactLoading ? (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Checking impact…
          </div>
        ) : isTier2 ? (
          <div className="space-y-3 py-1">
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertTitle>This credential is in use</AlertTitle>
              <AlertDescription>
                It is provided by the publisher in published bundle
                {bundleUsages.length !== 1 ? "s" : ""}. Deleting it will
                degrade those bundles back to "user provides" — installers will
                have to supply their own AI credentials.
              </AlertDescription>
            </Alert>
            <div className="space-y-1.5">
              <h4 className="text-sm font-medium">Affected bundles</h4>
              <ul className="space-y-1.5">
                {bundleUsages.map((usage) => (
                  <li
                    key={usage.bundle_uuid}
                    className="flex items-center justify-between gap-3 rounded-md border bg-muted/30 px-3 py-2"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <Box className="h-4 w-4 text-muted-foreground shrink-0" />
                      <div className="min-w-0">
                        <div className="text-sm font-medium truncate">
                          {usage.display_name}
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {[
                            usage.used_for_conversation && "conversation",
                            usage.used_for_building && "building",
                          ]
                            .filter(Boolean)
                            .join(" & ")}
                        </div>
                      </div>
                    </div>
                    {usage.publisher_install_id && (
                      <Button asChild variant="outline" size="sm">
                        <Link
                          to="/agent/$agentId"
                          params={{ agentId: usage.publisher_install_id }}
                          hash="bundle"
                        >
                          Open
                        </Link>
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        ) : null}

        <DialogFooter className="mt-4">
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={deleteMutation.isPending}
          >
            Cancel
          </Button>
          {isTier2 ? (
            <LoadingButton
              variant="destructive"
              loading={deleteMutation.isPending}
              disabled={impactLoading}
              onClick={() => deleteMutation.mutate(true)}
            >
              Force delete & degrade bundles
            </LoadingButton>
          ) : (
            <LoadingButton
              variant="destructive"
              loading={deleteMutation.isPending}
              disabled={impactLoading}
              onClick={() => deleteMutation.mutate(false)}
            >
              Delete
            </LoadingButton>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
