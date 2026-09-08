import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  CheckCircle2,
  EllipsisVertical,
  Pencil,
  Trash,
  UsersRound,
} from "lucide-react"
import { useState } from "react"

import {
  AdminLlmProvidersService,
  type ManagedAICredentialPublic,
} from "@/client"
import type { ApiError } from "@/client/core/ApiError"
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
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { ManagedCredentialDialog } from "./ManagedCredentialDialog"
import {
  type BlockedMember,
  blockedFromError,
  MANAGED_CREDENTIALS_QUERY_PREFIX,
} from "./providerTypes"

interface LlmProviderActionsMenuProps {
  record: ManagedAICredentialPublic
}

// Everything a mutation on this menu can move. Delete removes members'
// children and "set default for all" promotes them — and when the acting
// superuser is one of those members, the stale rows include their own.
// Invalidating only the admin list left them looking at a default in Settings
// that the server had already changed.
const AFFECTED_QUERY_KEYS = [
  MANAGED_CREDENTIALS_QUERY_PREFIX,
  ["currentUser"],
  ["aiCredentialsList"],
  ["aiCredentialsStatus"],
  ["resolveDefaultCredential"],
] as const

/**
 * The row menu for a managed credential, which has two shapes.
 *
 * * **Manual** — `Edit` · `Set default for all` · `Delete`. Three items.
 * * **Provider-owned** — `Members` (the same dialog, named for the one thing
 *   it can change) · `Set default for all`. Two items.
 *
 * Two items are absent from the provider-owned branch on purpose, and both
 * absences are structural rather than cosmetic:
 *
 * * **`Delete`** — `ManagedAICredentialsService.delete` refuses a
 *   provider-owned record with a 400 naming its provider, because deleting it
 *   would leave the provider owning nothing. The dialog says where Delete
 *   lives instead (on the provider).
 * * **`Apply to existing users`** — the action moved onto the provider in
 *   §5.2, and the managed-credential route that used to serve it is not on the
 *   router any more. It is gone from the **manual** branch too: a manual record
 *   has no `auto_provision_roles` to apply, so the call could only ever have
 *   answered `candidate_count: 0`, and an honest no-op is still a no-op.
 */
export function LlmProviderActionsMenu({
  record,
}: LlmProviderActionsMenuProps) {
  const [isEditOpen, setIsEditOpen] = useState(false)
  const [isDeleteOpen, setIsDeleteOpen] = useState(false)
  // Populated when a non-forced delete is blocked by bundle usage (409); drives
  // the "force delete" confirmation copy.
  const [blocked, setBlocked] = useState<BlockedMember[] | null>(null)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const isProviderOwned = record.is_provider_owned === true

  // Fire-and-forget on purpose, and the same way at every call site: these
  // mutations all close their dialog on success, so holding `isPending`
  // through a refetch would only delay the button going back to normal — and
  // on the delete path it would delay the 409 "force delete" affordance the
  // admin is waiting for.
  const invalidate = () => {
    for (const queryKey of AFFECTED_QUERY_KEYS) {
      void queryClient.invalidateQueries({ queryKey })
    }
  }

  const memberLabelById = new Map(
    (record.members ?? []).map((m) => [
      m.user_id,
      m.full_name ? `${m.full_name} <${m.email}>` : m.email,
    ]),
  )
  const labelFor = (userId: string) => memberLabelById.get(userId) ?? userId

  const setDefaultMutation = useMutation({
    mutationFn: () =>
      AdminLlmProvidersService.setManagedAiCredentialDefault({
        managedCredentialId: record.id,
      }),
    onSuccess: () => {
      showSuccessToast("Set as the default credential for all members.")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => invalidate(),
  })

  const deleteMutation = useMutation({
    mutationFn: (force: boolean) =>
      AdminLlmProvidersService.deleteManagedAiCredential({
        managedCredentialId: record.id,
        force,
      }),
    onSuccess: () => {
      showSuccessToast("Managed credential deleted.")
      setBlocked(null)
      setIsDeleteOpen(false)
    },
    onError: (error) => {
      const blockedMembers = blockedFromError(error)
      if (blockedMembers) {
        // Surface the Tier-2 block: keep the dialog open and switch it into the
        // "force delete" confirmation (mirrors the AI-credential Tier-2 flow).
        setBlocked(blockedMembers)
        showErrorToast(
          "One or more members could not be removed. Review below before forcing.",
        )
        return
      }
      handleError.call(showErrorToast, error as ApiError)
    },
    onSettled: () => invalidate(),
  })

  const isBlocked = (blocked?.length ?? 0) > 0

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 w-7 p-0"
            aria-label={`Actions for the credential ${record.name}`}
          >
            <EllipsisVertical className="h-4 w-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onClick={() => setIsEditOpen(true)}>
            {isProviderOwned ? <UsersRound /> : <Pencil />}
            {isProviderOwned ? "Members" : "Edit"}
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() => setDefaultMutation.mutate()}
            disabled={setDefaultMutation.isPending}
          >
            <CheckCircle2 />
            Set default for all
          </DropdownMenuItem>
          {!isProviderOwned && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                variant="destructive"
                onClick={() => {
                  setBlocked(null)
                  setIsDeleteOpen(true)
                }}
              >
                <Trash />
                Delete
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      {/* The unified edit dialog, which picks its own composition from
          `is_provider_owned`. Mounted only while open: every row of the table
          renders one of these, and an always-mounted instance runs the whole
          manual form's hooks — `useForm`, five `watch` subscriptions, the
          test-connection mutation — once per row, and would re-seed itself from
          a background refetch mid-edit. */}
      {isEditOpen && (
        <ManagedCredentialDialog
          mode="edit"
          record={record}
          open
          onOpenChange={setIsEditOpen}
        />
      )}

      {/* Delete confirm — escalates to a force confirmation on a 409 block */}
      <AlertDialog
        open={isDeleteOpen}
        onOpenChange={(open) => {
          setIsDeleteOpen(open)
          if (!open) setBlocked(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {isBlocked
                ? "Some members could not be removed"
                : "Delete managed credential"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {isBlocked ? (
                <>
                  Some members couldn't be removed — each reason is listed
                  below. Forcing the delete removes the record anyway; where the
                  cause is a published bundle, that bundle degrades back to
                  "user provides". This action cannot be undone.
                </>
              ) : (
                <>
                  Delete "{record.name}" and every member's credential
                  {record.member_count
                    ? ` (${record.member_count} member${record.member_count === 1 ? "" : "s"})`
                    : ""}
                  ? This action cannot be undone.
                </>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>

          {isBlocked && (
            <ul className="space-y-1 text-sm">
              {blocked!.map((b) => (
                <li
                  key={b.user_id}
                  className="rounded-md border bg-muted/30 px-3 py-1.5 text-muted-foreground"
                >
                  <span className="text-foreground">{labelFor(b.user_id)}</span>
                  {b.message ? ` — ${b.message}` : null}
                </li>
              ))}
            </ul>
          )}

          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                deleteMutation.mutate(isBlocked)
              }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteMutation.isPending
                ? "Deleting..."
                : isBlocked
                  ? "Force delete & degrade bundles"
                  : "Delete"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
