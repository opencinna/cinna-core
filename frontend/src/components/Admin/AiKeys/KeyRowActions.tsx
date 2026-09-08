import { useMutation, useQueryClient } from "@tanstack/react-query"
import { CheckCircle2, RefreshCw, RotateCw, Trash } from "lucide-react"
import { useState } from "react"

import { type AdminAIKeyRow, AdminAiKeysService } from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import {
  type BlockedMember,
  blockedFromError,
  MANAGED_CREDENTIALS_QUERY_PREFIX,
} from "../LlmProviders/providerTypes"
import { AI_KEYS_QUERY_PREFIX } from "./aiKeys"

/**
 * The `⋯` for one **per-user** key: retry, set default, rotate, revoke.
 *
 * Every item is a verb on one membership id. Before these routes existed the
 * only way to act on one person's key was to PATCH the record with the whole
 * desired member set, which is why the removal gate still answers with a list.
 *
 * The two confirms are owned by this component rather than by the menu items
 * that open them: a `DropdownMenuItem` unmounts on select and would take a
 * nested trigger's pending state with it (guidelines §2, "Row actions").
 */
export function KeyRowActions({ row }: { row: AdminAIKeyRow }) {
  const membershipId = row.membership_id
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [confirm, setConfirm] = useState<"revoke" | "rotate" | null>(null)
  // Set only by a 409 on revoke: the impact is knowable only after the attempt,
  // so the dialog stays open and swaps its body for what came back.
  const [blocked, setBlocked] = useState<BlockedMember[] | null>(null)

  const holder = row.holder_email ?? "this member"

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: AI_KEYS_QUERY_PREFIX })
    // The record list states a member count and a per-status summary computed
    // from the same rows. Leaving it alone would leave it stating a number that
    // stopped being true the moment this verb returned.
    void queryClient.invalidateQueries({
      queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX,
    })
  }

  const retryMutation = useMutation({
    mutationFn: () =>
      AdminAiKeysService.retryAiKey({ membershipId: membershipId! }),
    onSuccess: () => showSuccessToast("Key creation queued again."),
    onError: (error: ApiError) => handleError.call(showErrorToast, error),
    onSettled: invalidate,
  })

  const defaultMutation = useMutation({
    mutationFn: () =>
      AdminAiKeysService.setAiKeyAsDefault({ membershipId: membershipId! }),
    onSuccess: () =>
      showSuccessToast(`This is now the default key for ${holder}.`),
    onError: (error: ApiError) => handleError.call(showErrorToast, error),
    onSettled: invalidate,
  })

  const rotateMutation = useMutation({
    mutationFn: () =>
      AdminAiKeysService.rotateAiKey({ membershipId: membershipId! }),
    onSuccess: () => {
      showSuccessToast(
        "Key rotation queued. A new key arrives within a minute.",
      )
      setConfirm(null)
    },
    onError: (error: ApiError) => handleError.call(showErrorToast, error),
    onSettled: invalidate,
  })

  const revokeMutation = useMutation({
    mutationFn: (force: boolean) =>
      AdminAiKeysService.revokeAiKey({ membershipId: membershipId!, force }),
    onSuccess: () => {
      showSuccessToast(`The key for ${holder} was revoked.`)
      setConfirm(null)
      setBlocked(null)
    },
    onError: (error: ApiError) => {
      const rows = blockedFromError(error)
      if (rows?.length) {
        // Keep the dialog open and show what is in the way. A toast here would
        // close the only surface that can offer the way out.
        setBlocked(rows)
        return
      }
      handleError.call(showErrorToast, error)
    },
    onSettled: invalidate,
  })

  if (!membershipId) return null

  const busy =
    retryMutation.isPending ||
    defaultMutation.isPending ||
    rotateMutation.isPending ||
    revokeMutation.isPending
  // `mint_in_flight` has no way out but waiting: forcing during a mint is the
  // one action that can strand a key at the provider with nothing naming it.
  const forceable =
    blocked?.every((entry) => entry.reason !== "mint_in_flight") ?? false

  return (
    <>
      <RowActionsMenu label={`the key for ${holder}`} disabled={busy}>
        {row.provisioning_status === "failed" && (
          <DropdownMenuItem onClick={() => retryMutation.mutate()}>
            <RefreshCw />
            Retry
          </DropdownMenuItem>
        )}
        <DropdownMenuItem
          onClick={() => defaultMutation.mutate()}
          disabled={!row.child_credential_id || row.is_default}
        >
          <CheckCircle2 />
          Set as their default
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => setConfirm("rotate")}>
          <RotateCw />
          Rotate key
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          variant="destructive"
          onClick={() => {
            setBlocked(null)
            setConfirm("revoke")
          }}
        >
          <Trash />
          Revoke key
        </DropdownMenuItem>
      </RowActionsMenu>

      <AlertDialog
        open={confirm === "rotate"}
        onOpenChange={(open) => !open && setConfirm(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Rotate the key for {holder}?</AlertDialogTitle>
            <AlertDialogDescription>
              Their current key is destroyed at the provider straight away. A
              new one is created within a minute, and until it arrives they have
              no key.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={rotateMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              disabled={rotateMutation.isPending}
              onClick={(event) => {
                event.preventDefault()
                rotateMutation.mutate()
              }}
            >
              Rotate key
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={confirm === "revoke"}
        onOpenChange={(open) => {
          if (!open) {
            setConfirm(null)
            setBlocked(null)
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Revoke the key for {holder}?</AlertDialogTitle>
            <AlertDialogDescription>
              {blocked
                ? "This key could not be removed:"
                : "Their credential is deleted and the key is destroyed at the provider. This cannot be undone."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {blocked && (
            <ul className="space-y-1 text-sm text-muted-foreground">
              {blocked.map((entry) => (
                <li key={`${entry.reason}-${entry.user_id}`}>
                  {entry.message}
                </li>
              ))}
            </ul>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={revokeMutation.isPending}>
              {blocked && !forceable ? "Close" : "Cancel"}
            </AlertDialogCancel>
            {(!blocked || forceable) && (
              <AlertDialogAction
                disabled={revokeMutation.isPending}
                className="bg-destructive text-white hover:bg-destructive/90"
                onClick={(event) => {
                  event.preventDefault()
                  revokeMutation.mutate(Boolean(blocked))
                }}
              >
                {blocked ? "Revoke anyway" : "Revoke key"}
              </AlertDialogAction>
            )}
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
