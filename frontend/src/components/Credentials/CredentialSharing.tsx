import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, Share2 } from "lucide-react"
import { useEffect, useState } from "react"
import type { CredentialPublic } from "@/client"
import { CredentialsService } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import { PreviewList } from "@/components/Common/PreviewList"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import useCustomToast from "@/hooks/useCustomToast"
import useRole from "@/hooks/useRole"
import { handleError } from "@/utils"
import { isLocallyDeliverableCredentialType } from "@/utils/credentialLocalUse"
import {
  bundleImpactSentence,
  skillImpactSentence,
} from "@/utils/skillCredentials"
import { CredentialUsagesSheet } from "./CredentialUsagesSheet"
import { BundleUsageRow, SkillUsageRow } from "./CredentialUsageRows"

interface CredentialSharingProps {
  credential: CredentialPublic
}

export function CredentialSharing({ credential }: CredentialSharingProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { isAgentUser } = useRole()
  const [isDisableDialogOpen, setIsDisableDialogOpen] = useState(false)
  const [allBundlesOpen, setAllBundlesOpen] = useState(false)
  const [allSkillsOpen, setAllSkillsOpen] = useState(false)
  const [allowSharing, setAllowSharing] = useState(
    credential.allow_sharing ?? false,
  )

  // Sync local state when prop changes (e.g., after query refetch)
  useEffect(() => {
    setAllowSharing(credential.allow_sharing ?? false)
  }, [credential.allow_sharing])

  const { data: sharesData } = useQuery({
    queryKey: ["credential-shares", credential.id],
    queryFn: () =>
      CredentialsService.getCredentialShares({ credentialId: credential.id }),
    enabled: allowSharing,
  })

  // Bundles whose publisher install has this credential linked. Shown
  // on the sharing card so the owner can see at a glance where their
  // credential is in use across the bundles they publish.
  const {
    data: bundleUsages,
    isLoading: isBundleUsagesLoading,
    isError: isBundleUsagesError,
    error: bundleUsagesError,
    refetch: refetchBundleUsages,
  } = useQuery({
    queryKey: ["credential-bundle-usages", credential.id],
    queryFn: () =>
      CredentialsService.listCredentialBundleUsages({ id: credential.id }),
  })

  // Deletion-impact also covers the disable-sharing blast radius: when this
  // credential is publisher-provided (PBP) in published bundles or catalog
  // skills, disabling sharing revokes the publisher shares and breaks those
  // installs — the same class of impact as a delete. Read by the
  // disable-sharing dialog, the delete dialog (same cache key) and the card's
  // "Used in Skills" list.
  const {
    data: deletionImpact,
    isLoading: isImpactLoading,
    isError: isImpactError,
    error: impactError,
    refetch: refetchImpact,
  } = useQuery({
    queryKey: ["credential-deletion-impact", credential.id],
    queryFn: () =>
      CredentialsService.getCredentialDeletionImpact({ id: credential.id }),
    // Un-gated, unlike the dialogs it also feeds: the skill usages are on the
    // card body now, and the bundle usages beside them have always been an
    // always-on read. Both dialogs therefore open with the data already warm
    // rather than on a spinner.
  })

  // Invalidate every cache that carries this credential's share_count so the
  // "Shared with N users" header and card badge update immediately.
  const invalidateShareCaches = () => {
    queryClient.invalidateQueries({
      queryKey: ["credential-shares", credential.id],
    })
    queryClient.invalidateQueries({ queryKey: ["credentials"] })
    queryClient.invalidateQueries({ queryKey: ["credential", credential.id] })
    queryClient.invalidateQueries({
      queryKey: ["credential-with-data", credential.id],
    })
  }

  const shareMutation = useMutation({
    mutationFn: (email: string) =>
      CredentialsService.shareCredential({
        credentialId: credential.id,
        requestBody: { shared_with_email: email },
      }),
    onSuccess: () => {
      showSuccessToast("Credential shared successfully")
      invalidateShareCaches()
    },
    onError: handleError.bind(showErrorToast),
  })

  const revokeMutation = useMutation({
    mutationFn: (shareId: string) =>
      CredentialsService.revokeCredentialShare({
        credentialId: credential.id,
        shareId,
      }),
    onSuccess: () => {
      showSuccessToast("Share revoked successfully")
      invalidateShareCaches()
    },
    onError: handleError.bind(showErrorToast),
  })

  const localUseMutation = useMutation({
    mutationFn: (allow_local_use: boolean) => CredentialsService.updateCredential({
      id: credential.id, requestBody: { allow_local_use },
    }),
    onSuccess: () => { invalidateShareCaches(); queryClient.invalidateQueries({ queryKey: ["credential", credential.id] }) },
    onError: handleError.bind(showErrorToast),
  })

  const toggleSharingMutation = useMutation({
    mutationFn: (newAllowSharing: boolean) =>
      CredentialsService.updateCredentialSharing({
        credentialId: credential.id,
        requestBody: { allow_sharing: newAllowSharing },
      }),
    onSuccess: (_, newAllowSharing) => {
      setAllowSharing(newAllowSharing)
      showSuccessToast(
        newAllowSharing
          ? "Sharing enabled for this credential"
          : "Sharing disabled. All shares have been revoked.",
      )
      setIsDisableDialogOpen(false)
      invalidateShareCaches()
    },
    onError: handleError.bind(showErrorToast),
  })

  const shares = sharesData?.data ?? []
  const shareCount = credential.share_count ?? 0
  const isDisabling = toggleSharingMutation.isPending

  // Map existing shares into the shared picker's selected-pill model.
  const selectedShares: UserAllowlistSelectedItem[] = shares.map((s) => ({
    id: s.id, // share id — revoke endpoint key
    userId: s.shared_with_user_id,
    fallbackLabel: s.shared_with_email,
  }))

  const sharedBundleUsages = (bundleUsages?.data ?? []).filter(
    (u) => u.provided_by === "publisher",
  )
  // "Down" is an error with nothing cached: a failed background refetch keeps
  // the rows it already had.
  const bundleUsagesDown = isBundleUsagesError && !bundleUsages
  // The section is hidden only once it is known to be empty; while loading or
  // failed it shows, so a failure never reads as "used in no bundles".
  const showBundleUsages =
    isBundleUsagesLoading || bundleUsagesDown || sharedBundleUsages.length > 0

  // What disabling sharing breaks, composed per source: a sentence only for a
  // source whose published packages have active installs (nothing breaks
  // otherwise), one alert for both, and one list of those sources' rows.
  const pbpBundleUsages = deletionImpact?.bundle_pbp_usages ?? []
  const pbpSkillUsages = deletionImpact?.skill_pbp_usages ?? []
  // Same two rules as the bundles list above: a failed background refetch
  // keeps the rows it had, and the section hides only once it is known to be
  // empty, so a failure never reads as "used in no skills".
  const impactDown = isImpactError && !deletionImpact
  const showSkillUsages =
    isImpactLoading || impactDown || pbpSkillUsages.length > 0
  const activeBundleInstalls = deletionImpact?.active_install_count ?? 0
  const activeSkillInstalls = deletionImpact?.active_skill_install_count ?? 0
  const bundlesBreak = pbpBundleUsages.length > 0 && activeBundleInstalls > 0
  const skillsBreak = pbpSkillUsages.length > 0 && activeSkillInstalls > 0
  const bundleImpact = bundlesBreak
    ? bundleImpactSentence(
        pbpBundleUsages.length,
        activeBundleInstalls,
        "disable",
      )
    : null
  const skillImpact = skillsBreak
    ? skillImpactSentence(pbpSkillUsages.length, activeSkillInstalls, "disable")
    : null

  if (isAgentUser) {
    return null
  }

  const handleToggleSharing = (checked: boolean) => {
    if (!checked && shareCount > 0) {
      // Show confirmation dialog before disabling
      setIsDisableDialogOpen(true)
    } else {
      toggleSharingMutation.mutate(checked)
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2">
              <Share2 className="h-5 w-5" />
              Sharing
            </CardTitle>
            <CardDescription>
              {allowSharing
                ? "Share this credential with other users to allow them to use it in their agents."
                : "Enable to share this credential with other users."}
            </CardDescription>
          </div>
          <Switch
            checked={allowSharing}
            onCheckedChange={handleToggleSharing}
            disabled={toggleSharingMutation.isPending}
            aria-label="Allow credential sharing"
            className="ml-4 mt-1"
          />
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {allowSharing && isLocallyDeliverableCredentialType(credential.type) && (
          <div className="flex items-start justify-between gap-4">
            <div><p className="text-sm font-medium">Allow use on recipients’ computers</p>
              <p className="text-sm text-muted-foreground">People you share this with can use it in agents on their own computer. The value is stored on their machine.</p>
              <p className="text-sm text-muted-foreground">Disabling this cannot recall values already delivered.</p>
            </div>
            <Switch aria-label="Allow use on recipients’ computers" checked={credential.allow_local_use ?? false}
              disabled={localUseMutation.isPending} onCheckedChange={value => localUseMutation.mutate(value)} />
          </div>
        )}
        {allowSharing && (
          <div className="space-y-3">
            <h4 className="text-sm font-medium">
              Shared with {shareCount} user{shareCount !== 1 ? "s" : ""}
            </h4>
            <UserAllowlistPicker
              label={null}
              selected={selectedShares}
              onAdd={(u) => shareMutation.mutate(u.email)}
              onRemove={(item) => revokeMutation.mutate(item.id)}
              isAdding={shareMutation.isPending}
              isRemoving={revokeMutation.isPending}
              searchPlaceholder="Search users by name or email..."
              emptyHint="This credential is not shared with anyone yet."
            />
            <p className="text-xs text-muted-foreground">
              Recipients can use this credential in their agents but won't see
              the actual credential values.
            </p>
          </div>
        )}

        {showBundleUsages && (
          <div className="space-y-2 pt-2">
            <h4 className="text-sm font-medium">Used in Bundles</h4>
            <p className="text-xs text-muted-foreground">
              Bundles that ship this credential as a fully shared publisher
              credential.
            </p>
            {/* P5: capped at the house five, the rest in a Sheet. */}
            <PreviewList
              items={sharedBundleUsages}
              getKey={(usage) => usage.bundle_uuid}
              renderItem={(usage) => <BundleUsageRow usage={usage} />}
              isLoading={isBundleUsagesLoading}
              isError={bundleUsagesDown}
              error={bundleUsagesError}
              onRetry={() => refetchBundleUsages()}
              errorFallback="Couldn't load the bundles that use this credential"
              empty={null}
              skeletonRows={1}
              skeletonClassName="h-[44px] w-full rounded-md"
              onShowAll={() => setAllBundlesOpen(true)}
            />
          </div>
        )}

        {showSkillUsages && (
          <div className="space-y-2 pt-2">
            <h4 className="text-sm font-medium">Used in Skills</h4>
            <p className="text-xs text-muted-foreground">
              Published skills that ship this credential as a fully shared
              publisher credential.
            </p>
            {/* P5: capped at the house five, the rest in a Sheet. */}
            <PreviewList
              items={pbpSkillUsages}
              getKey={(usage) => usage.package_uuid}
              renderItem={(usage) => <SkillUsageRow usage={usage} />}
              isLoading={isImpactLoading}
              isError={impactDown}
              error={impactError}
              onRetry={() => refetchImpact()}
              errorFallback="Couldn't load the skills that use this credential"
              empty={null}
              skeletonRows={1}
              skeletonClassName="h-[44px] w-full rounded-md"
              onShowAll={() => setAllSkillsOpen(true)}
            />
          </div>
        )}

        <CredentialUsagesSheet
          title="Used in bundles"
          description={`${sharedBundleUsages.length} bundle${
            sharedBundleUsages.length === 1 ? "" : "s"
          } that ship this credential as a fully shared publisher credential`}
          open={allBundlesOpen}
          onOpenChange={setAllBundlesOpen}
        >
          {sharedBundleUsages.map((usage) => (
            <BundleUsageRow key={usage.bundle_uuid} usage={usage} />
          ))}
        </CredentialUsagesSheet>

        <CredentialUsagesSheet
          title="Used in skills"
          description={`${pbpSkillUsages.length} published skill${
            pbpSkillUsages.length === 1 ? "" : "s"
          } that ship this credential as a fully shared publisher credential`}
          open={allSkillsOpen}
          onOpenChange={setAllSkillsOpen}
        >
          {pbpSkillUsages.map((usage) => (
            <SkillUsageRow key={usage.package_uuid} usage={usage} />
          ))}
        </CredentialUsagesSheet>

        {/* Disable-sharing confirm. A `Dialog` rather than an `AlertDialog`
            because it carries fetched impact data (§2 Confirmation). */}
        <Dialog
          open={isDisableDialogOpen}
          onOpenChange={(next) => {
            // Escape and outside-click must not close a dialog mid-request.
            if (!isDisabling) setIsDisableDialogOpen(next)
          }}
        >
          <DialogContent className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-md [&>*]:min-w-0">
            <DialogHeader>
              <DialogTitle className="break-words">
                Disable sharing for {credential.name}?
              </DialogTitle>
              <DialogDescription>
                This will revoke access for all users this credential is
                currently shared with.
              </DialogDescription>
            </DialogHeader>
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertTitle>Warning</AlertTitle>
              <AlertDescription>
                {shareCount} user{shareCount !== 1 ? "s" : ""} will lose access
                to this credential immediately. This action cannot be undone.
              </AlertDescription>
            </Alert>
            {isImpactLoading ? (
              <div className="space-y-2">
                <Skeleton className="h-[44px] w-full" />
                <Skeleton className="h-[44px] w-full" />
              </div>
            ) : impactDown ? (
              // Revoking access must not be blocked by a failed read, so the
              // confirm below stays enabled.
              <QueryErrorAlert
                error={impactError}
                fallback="Couldn't check which bundles and skills use it"
                onRetry={() => refetchImpact()}
                compact
              />
            ) : (
              (bundleImpact || skillImpact) && (
                <div className="space-y-2">
                  <Alert variant="destructive">
                    <AlertTriangle className="h-4 w-4" />
                    <AlertTitle>
                      Published installs lose this credential
                    </AlertTitle>
                    <AlertDescription>
                      {[bundleImpact, skillImpact].filter(Boolean).join(" ")}
                    </AlertDescription>
                  </Alert>
                  <ListRowGroup>
                    {bundlesBreak &&
                      pbpBundleUsages.map((usage) => (
                        <BundleUsageRow key={usage.bundle_uuid} usage={usage} />
                      ))}
                    {skillsBreak &&
                      pbpSkillUsages.map((usage) => (
                        <SkillUsageRow key={usage.package_uuid} usage={usage} />
                      ))}
                  </ListRowGroup>
                </div>
              )
            )}
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setIsDisableDialogOpen(false)}
                disabled={isDisabling}
              >
                Cancel
              </Button>
              <LoadingButton
                variant="destructive"
                loading={isDisabling}
                // Nobody confirms blind while the impact is still loading.
                disabled={isImpactLoading}
                onClick={() => toggleSharingMutation.mutate(false)}
              >
                Disable sharing
              </LoadingButton>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  )
}
