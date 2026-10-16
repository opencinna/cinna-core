import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle } from "lucide-react"
import { useState } from "react"

import {
  type CredentialDeletionImpact,
  type CredentialPublic,
  CredentialsService,
} from "@/client"
import { ApiError } from "@/client/core/ApiError"
import { AgentBadge } from "@/components/Common/AgentBadge"
import { ListRowGroup } from "@/components/Common/ListRow"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
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
import { LoadingButton } from "@/components/ui/loading-button"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import {
  bundleImpactSentence,
  skillImpactSentence,
} from "@/utils/skillCredentials"
import { BundleUsageRow, SkillUsageRow } from "./CredentialUsageRows"
import { localSkillImpactSentence } from "./localSkillImpact"

interface DeleteCredentialProps {
  /** Only the id and the name are read, so a list row's view-model will do. */
  credential: Pick<CredentialPublic, "id" | "name">
  onSuccess: () => void
  isOpen?: boolean
  setIsOpen?: (open: boolean) => void
  /**
   * The trigger. Omit it to drive the dialog purely through `isOpen` — the
   * shape a list uses, holding one `deleteTarget` for all its rows: a trigger
   * inside a `DropdownMenuItem` unmounts with the menu, and takes the dialog's
   * state with it (§2 Row actions).
   */
  children?: React.ReactNode
}

/**
 * The key a credential's delete runs under, so a list can show that row as
 * pending (`useIsMutating`) without owning the mutation.
 */
export function deleteCredentialMutationKey(credentialId: string) {
  return ["delete-credential", credentialId] as const
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
  const impactQueryKey = ["credential-deletion-impact", credential.id]

  // Fetch the deletion blast-radius once the dialog opens.
  const {
    data: impact,
    isLoading: impactLoading,
    isError: isImpactError,
    error: impactError,
    refetch: refetchImpact,
  } = useQuery({
    queryKey: impactQueryKey,
    queryFn: () =>
      CredentialsService.getCredentialDeletionImpact({ id: credential.id }),
    enabled: isOpen,
  })

  const mutation = useMutation({
    mutationKey: deleteCredentialMutationKey(credential.id),
    mutationFn: (force: boolean) =>
      CredentialsService.deleteCredential({ id: credential.id, force }),
    onSuccess: () => {
      showSuccessToast("The credential was deleted successfully")
      setIsOpen(false)
      onSuccess()
    },
    onError: (error) => {
      // A non-forced delete can race a bundle or skill install and come back
      // 409. The 409 body *is* the impact: cache it, so "Review the impact
      // below" always has something below it and the forced delete is offered
      // even when the impact read itself had failed.
      const conflict = impactFromError(error)
      if (conflict) {
        queryClient.setQueryData(impactQueryKey, conflict)
        showErrorToast(
          "This credential is now in use by a published bundle or skill. Review the impact below.",
        )
        return
      }
      handleError.bind(showErrorToast)(error as ApiError)
    },
    // Returned, so the mutation (and the row's pending state) lasts until the
    // refetch lands — otherwise the deleted row briefly comes back live.
    onSettled: () => queryClient.invalidateQueries(),
  })

  const tier = impact?.tier ?? 0
  const isTier2 = tier === 2
  const affectedAgents = impact?.affected_own_agents ?? []
  const shareCount = impact?.direct_share_count ?? 0
  const pbpUsages = impact?.bundle_pbp_usages ?? []
  const bundleUsages = impact?.bundle_usages ?? []
  const activeInstallCount = impact?.active_install_count ?? 0
  const skillUsages = impact?.skill_pbp_usages ?? []
  const activeSkillInstallCount = impact?.active_skill_install_count ?? 0
  const localSkillInstallers = impact?.active_local_skill_install_count ?? 0

  // Tier 2 is reached through bundles, skills, or both, so the alert says only
  // what is true: each source's sentence appears only when that source has
  // active installs — a credential at tier 2 because of skills must not read
  // "bundles with 0 active installs".
  const bundleTierSentence =
    pbpUsages.length > 0 && activeInstallCount > 0
      ? bundleImpactSentence(pbpUsages.length, activeInstallCount, "delete")
      : null
  const skillTierSentence =
    skillUsages.length > 0 && activeSkillInstallCount > 0
      ? skillImpactSentence(
          skillUsages.length,
          activeSkillInstallCount,
          "delete",
        )
      : null
  // Tier 2 can come from skills installed on recipients' computers alone.
  const localSkillTierSentence =
    localSkillInstallers > 0
      ? localSkillImpactSentence(localSkillInstallers, "delete")
      : null

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(next) => {
        // Escape and outside-click must not close a dialog mid-request.
        if (!mutation.isPending) setIsOpen(next)
      }}
    >
      {children && <DialogTrigger asChild>{children}</DialogTrigger>}
      <DialogContent className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-md [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle className="break-words">
            Delete {credential.name}?
          </DialogTitle>
          <DialogDescription>
            This credential will be permanently deleted. You will not be able to
            undo this action.
          </DialogDescription>
        </DialogHeader>

        {impactLoading ? (
          <div className="space-y-2 py-1">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-[44px] w-full" />
          </div>
        ) : isImpactError && !impact ? (
          // The non-forced Delete stays enabled: a tier-2 credential comes back
          // 409, and the handler above caches its impact then.
          <QueryErrorAlert
            error={impactError}
            fallback="Couldn't check which bundles and skills use it"
            onRetry={() => refetchImpact()}
            compact
          />
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
                  {affectedAgents.map((agent) => (
                    <AgentBadge key={agent.id} agent={agent} size="md" />
                  ))}
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
                  {shareCount !== 1 ? "s" : ""} will lose access to this
                  credential immediately.
                </AlertDescription>
              </Alert>
            )}

            {/* Tier 2: publisher-provided in published bundles or skills with installs */}
            {isTier2 &&
              (bundleTierSentence ||
                skillTierSentence ||
                localSkillTierSentence) && (
                <Alert variant="destructive">
                  <AlertTriangle className="h-4 w-4" />
                  <AlertTitle>This credential is in use</AlertTitle>
                  <AlertDescription>
                    {[
                      bundleTierSentence,
                      skillTierSentence,
                      localSkillTierSentence,
                    ]
                      .filter(Boolean)
                      .join(" ")}
                  </AlertDescription>
                </Alert>
              )}

            {/* All tiers: the bundles and skills this credential is part of */}
            {bundleUsages.length > 0 && (
              <div className="space-y-1.5">
                <h4 className="text-sm font-medium">Used in bundles</h4>
                <ListRowGroup>
                  {bundleUsages.map((usage) => (
                    <BundleUsageRow
                      key={usage.bundle_uuid}
                      usage={usage}
                      showProvidedBy
                    />
                  ))}
                </ListRowGroup>
              </div>
            )}
            {skillUsages.length > 0 && (
              <div className="space-y-1.5">
                <h4 className="text-sm font-medium">Used in skills</h4>
                <ListRowGroup>
                  {skillUsages.map((usage) => (
                    <SkillUsageRow key={usage.package_uuid} usage={usage} />
                  ))}
                </ListRowGroup>
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
              Delete credential
            </LoadingButton>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default DeleteCredential
