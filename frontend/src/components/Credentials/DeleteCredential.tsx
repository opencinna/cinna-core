import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { AlertTriangle, Bot, Box, Loader2, Trash2 } from "lucide-react"
import { useState } from "react"

import {
  type CredentialBundleUsage,
  type CredentialDeletionImpact,
  type CredentialPublic,
  CredentialsService,
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
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { DropdownMenuItem } from "@/components/ui/dropdown-menu"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { getColorPreset } from "@/utils/colorPresets"
import { cn } from "@/lib/utils"

interface DeleteCredentialProps {
  credential: CredentialPublic
  onSuccess: () => void
  isOpen?: boolean
  setIsOpen?: (open: boolean) => void
  children?: React.ReactNode
}

/** Human-readable label for a usage's provisioning mode. */
function providedByLabel(providedBy: string): string {
  switch (providedBy) {
    case "publisher":
      return "Shared with installers"
    case "template":
      return "Template"
    case "user":
      return "User-provided"
    default:
      return providedBy
  }
}

/** A single bundle row: icon + name + bundle_id + mode badge + deep-link. */
function BundleUsageRow({ usage }: { usage: CredentialBundleUsage }) {
  return (
    <li className="flex items-center justify-between gap-3 rounded-md border bg-muted/30 px-3 py-2">
      <div className="flex items-center gap-2 min-w-0">
        <Box className="h-4 w-4 text-muted-foreground shrink-0" />
        <div className="min-w-0">
          <div className="text-sm font-medium truncate">
            {usage.display_name}
          </div>
          <div className="text-xs text-muted-foreground truncate font-mono">
            {usage.bundle_id}
          </div>
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <span className="text-xs text-muted-foreground rounded-md border bg-background px-2 py-1">
          {providedByLabel(usage.provided_by)}
        </span>
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
      </div>
    </li>
  )
}

/** Extract a CredentialDeletionImpact from a 409 ApiError body, if present. */
function impactFromError(error: unknown): CredentialDeletionImpact | null {
  if (error instanceof ApiError && error.status === 409) {
    const detail = (error.body as { detail?: unknown } | undefined)?.detail
    if (detail && typeof detail === "object" && "tier" in detail) {
      return detail as CredentialDeletionImpact
    }
  }
  return null
}

const DeleteCredential = ({
  credential,
  onSuccess,
  isOpen: controlledIsOpen,
  setIsOpen: controlledSetIsOpen,
  children,
}: DeleteCredentialProps) => {
  const [uncontrolledIsOpen, setUncontrolledIsOpen] = useState(false)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const isOpen = controlledIsOpen ?? uncontrolledIsOpen
  const setIsOpen = controlledSetIsOpen ?? setUncontrolledIsOpen

  // Fetch the deletion blast-radius once the dialog opens.
  const { data: impact, isLoading: impactLoading } = useQuery({
    queryKey: ["credential-deletion-impact", credential.id],
    queryFn: () =>
      CredentialsService.getCredentialDeletionImpact({ id: credential.id }),
    enabled: isOpen,
  })

  const mutation = useMutation({
    mutationFn: (force: boolean) =>
      CredentialsService.deleteCredential({ id: credential.id, force }),
    onSuccess: () => {
      showSuccessToast("The credential was deleted successfully")
      setIsOpen(false)
      onSuccess()
    },
    onError: (error) => {
      // A non-forced delete can race a bundle install and come back 409.
      // Surface the impact inline rather than a generic error toast.
      if (impactFromError(error)) {
        queryClient.invalidateQueries({
          queryKey: ["credential-deletion-impact", credential.id],
        })
        showErrorToast(
          "This credential is now in use by a published bundle. Review the impact below.",
        )
        return
      }
      handleError.bind(showErrorToast)(error as ApiError)
    },
    onSettled: () => {
      queryClient.invalidateQueries()
    },
  })

  const tier = impact?.tier ?? 0
  const isTier2 = tier === 2
  const affectedAgents = impact?.affected_own_agents ?? []
  const shareCount = impact?.direct_share_count ?? 0
  const pbpUsages = impact?.bundle_pbp_usages ?? []
  const bundleUsages = impact?.bundle_usages ?? []
  const activeInstallCount = impact?.active_install_count ?? 0

  return (
    <Dialog open={isOpen} onOpenChange={setIsOpen}>
      {children ? (
        <DialogTrigger asChild>{children}</DialogTrigger>
      ) : (
        <DropdownMenuItem
          variant="destructive"
          onSelect={(e) => e.preventDefault()}
          onClick={() => setIsOpen(true)}
        >
          <Trash2 />
          Delete Credential
        </DropdownMenuItem>
      )}
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Delete Credential</DialogTitle>
          <DialogDescription>
            This credential will be permanently deleted. You will not be able to
            undo this action.
          </DialogDescription>
        </DialogHeader>

        {impactLoading ? (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Checking impact…
          </div>
        ) : (
          <div className="space-y-3 py-1">
            {/* Tier 0: list affected own agents */}
            {tier === 0 && affectedAgents.length > 0 && (
              <div className="space-y-1.5">
                <p className="text-sm text-muted-foreground">
                  This credential is linked to the following agent
                  {affectedAgents.length !== 1 ? "s" : ""}:
                </p>
                <div className="flex flex-wrap gap-2">
                  {affectedAgents.map((agent) => {
                    const preset = getColorPreset(agent.ui_color_preset)
                    return (
                      <span
                        key={agent.id}
                        className={cn(
                          "px-3 py-1.5 text-sm rounded-md flex items-center gap-2",
                          preset.badgeBg,
                          preset.badgeText,
                        )}
                      >
                        <Bot className="h-3.5 w-3.5" />
                        {agent.name}
                      </span>
                    )
                  })}
                </div>
              </div>
            )}

            {/* Tier 1: direct shares warning */}
            {tier === 1 && (
              <Alert variant="destructive">
                <AlertTriangle className="h-4 w-4" />
                <AlertTitle>Warning</AlertTitle>
                <AlertDescription>
                  {shareCount} user
                  {shareCount !== 1 ? "s" : ""} will lose access
                  to this credential immediately.
                </AlertDescription>
              </Alert>
            )}

            {/* Tier 2: publisher-provided in published bundle(s) with installs */}
            {isTier2 && (
              <>
                <Alert variant="destructive">
                  <AlertTriangle className="h-4 w-4" />
                  <AlertTitle>This credential is in use</AlertTitle>
                  <AlertDescription>
                    It is provided by the publisher in published bundle
                    {pbpUsages.length !== 1 ? "s" : ""} with{" "}
                    {activeInstallCount} active install
                    {activeInstallCount !== 1 ? "s" : ""}. Deleting it
                    will break those installs — their owners will be told the
                    publisher-provided credentials are unavailable. Consider
                    rotating the credential value instead, or remove it from each
                    bundle first.
                  </AlertDescription>
                </Alert>
              </>
            )}

            {/* All tiers: the credential is part of one or more bundles */}
            {bundleUsages.length > 0 && (
              <div className="space-y-1.5">
                <h4 className="text-sm font-medium">Used in bundles</h4>
                <ul className="space-y-1.5">
                  {bundleUsages.map((usage) => (
                    <BundleUsageRow key={usage.bundle_uuid} usage={usage} />
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        <DialogFooter className="mt-4">
          <DialogClose asChild>
            <Button variant="outline" disabled={mutation.isPending}>
              Cancel
            </Button>
          </DialogClose>
          {isTier2 ? (
            <LoadingButton
              variant="destructive"
              loading={mutation.isPending}
              disabled={impactLoading}
              onClick={() => mutation.mutate(true)}
            >
              Force delete & break installs
            </LoadingButton>
          ) : (
            <LoadingButton
              variant="destructive"
              loading={mutation.isPending}
              disabled={impactLoading}
              onClick={() => mutation.mutate(false)}
            >
              Delete
            </LoadingButton>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default DeleteCredential
