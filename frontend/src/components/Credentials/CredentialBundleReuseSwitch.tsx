/**
 * "Allow re-use inside bundles" — the owner's consent for the people they
 * share an ``agent_api`` connection with to embed it, as publisher-provided,
 * in the bundles and skills those people publish (S1).
 *
 * A second instance of the Sharing card's local-use block: label, two lines,
 * an auto-saving `Switch`. Turning it off releases every share those
 * publishers' installers got, so with re-shared people present it confirms
 * first and states the count.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { type CredentialPublic, CredentialsService } from "@/client"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import { Switch } from "@/components/ui/switch"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

interface CredentialBundleReuseSwitchProps {
  credential: CredentialPublic
  /**
   * How many people got it through other people's bundles or skills, from
   * the card's share list; `null` while that list is not known, which
   * confirms rather than releases blind.
   */
  resharedCount: number | null
  /** The Sharing card's cache invalidation (shares, counts, the credential). */
  onChanged: () => Promise<unknown>
}

export function CredentialBundleReuseSwitch({
  credential,
  resharedCount,
  onChanged,
}: CredentialBundleReuseSwitchProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [confirmOpen, setConfirmOpen] = useState(false)

  const mutation = useMutation({
    mutationFn: (allow_bundle_reuse: boolean) =>
      CredentialsService.updateCredential({
        id: credential.id,
        requestBody: { allow_bundle_reuse },
      }),
    onSuccess: (_, allow) => {
      showSuccessToast(
        allow
          ? "Re-use inside bundles allowed"
          : "Re-use inside bundles stopped",
      )
      setConfirmOpen(false)
    },
    onError: handleError.bind(showErrorToast),
    // On both paths: a failed write snaps the switch back to the stored value.
    // Awaited, so the switch stays disabled until the credential has
    // refetched and never shows the old value as clickable.
    onSettled: () =>
      Promise.all([
        onChanged(),
        queryClient.invalidateQueries({
          queryKey: ["credential-deletion-impact", credential.id],
        }),
      ]),
  })

  const handleChange = (checked: boolean) => {
    if (!checked && resharedCount !== 0) setConfirmOpen(true)
    else mutation.mutate(checked)
  }

  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <p className="text-sm font-medium">Allow re-use inside bundles</p>
        <p className="text-sm text-muted-foreground">
          People you share this connection with can embed it in bundles and
          skills they publish; their installers get it from you.
        </p>
        <p className="text-sm text-muted-foreground">
          Installers lose it as soon as the publisher's own access ends.
        </p>
      </div>
      <Switch
        aria-label="Allow re-use inside bundles"
        checked={credential.allow_bundle_reuse ?? false}
        disabled={mutation.isPending}
        onCheckedChange={handleChange}
      />

      <AlertDialog
        open={confirmOpen}
        onOpenChange={(next) => {
          // Escape and outside-click must not close a dialog mid-request.
          if (!mutation.isPending) setConfirmOpen(next)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="break-words">
              Stop re-use of <em>{credential.name}</em> inside bundles?
            </AlertDialogTitle>
            <AlertDialogDescription>
              {resharedCount === null
                ? "Everyone who got it through other people's bundles loses it now"
                : `${resharedCount === 1 ? "1 person" : `${resharedCount} people`} who got it through other people's bundles ${resharedCount === 1 ? "loses" : "lose"} it now`}
              , and those bundles stop providing it on install and update.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={mutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <LoadingButton
              variant="destructive"
              loading={mutation.isPending}
              onClick={() => mutation.mutate(false)}
            >
              Stop re-use
            </LoadingButton>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
