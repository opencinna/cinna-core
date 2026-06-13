import { useMutation, useQueryClient } from "@tanstack/react-query"
import { CheckCircle2, EllipsisVertical, Pencil, Trash } from "lucide-react"
import { useState } from "react"

import {
  type ManagedAICredentialPublic,
  AdminLlmProvidersService,
} from "@/client"
import { ApiError } from "@/client/core/ApiError"
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
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { ManagedCredentialDialog } from "./ManagedCredentialDialog"
import { MANAGED_CREDENTIALS_QUERY_PREFIX } from "./providerTypes"

interface LlmProviderActionsMenuProps {
  record: ManagedAICredentialPublic
}

// One blocked member from a 409 delete response.
interface BlockedMember {
  user_id: string
  reason: string
  impact?: unknown
}

// Extract the blocked-members list from a 409 ApiError body
// ({ detail: { message, blocked: [...] } }).
function blockedFromError(error: unknown): BlockedMember[] | null {
  if (error instanceof ApiError && error.status === 409) {
    const detail = (error.body as { detail?: unknown } | undefined)?.detail
    if (detail && typeof detail === "object" && "blocked" in detail) {
      const blocked = (detail as { blocked?: unknown }).blocked
      if (Array.isArray(blocked)) return blocked as BlockedMember[]
    }
  }
  return null
}

export function LlmProviderActionsMenu({ record }: LlmProviderActionsMenuProps) {
  const [isEditOpen, setIsEditOpen] = useState(false)
  const [isDeleteOpen, setIsDeleteOpen] = useState(false)
  // Populated when a non-forced delete is blocked by bundle usage (409); drives
  // the "force delete" confirmation copy.
  const [blocked, setBlocked] = useState<BlockedMember[] | null>(null)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX })

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
    onSettled: () => void invalidate(),
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
          "One or more members are in use by a published bundle. Review below before forcing.",
        )
        return
      }
      handleError.call(showErrorToast, error as ApiError)
    },
    onSettled: () => void invalidate(),
  })

  const isBlocked = (blocked?.length ?? 0) > 0

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="sm" className="h-7 w-7 p-0">
            <EllipsisVertical className="h-4 w-4" />
            <span className="sr-only">Open menu</span>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onClick={() => setIsEditOpen(true)}>
            <Pencil className="mr-2 h-4 w-4" />
            Edit
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() => setDefaultMutation.mutate()}
            disabled={setDefaultMutation.isPending}
          >
            <CheckCircle2 className="mr-2 h-4 w-4" />
            Set default for all
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() => {
              setBlocked(null)
              setIsDeleteOpen(true)
            }}
            className="text-destructive focus:text-destructive"
          >
            <Trash className="mr-2 h-4 w-4" />
            Delete
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {/* Unified edit dialog */}
      <ManagedCredentialDialog
        mode="edit"
        record={record}
        open={isEditOpen}
        onOpenChange={setIsEditOpen}
      />

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
              {isBlocked ? "Members in use by a bundle" : "Delete Managed Credential"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {isBlocked ? (
                <>
                  Some members couldn't be removed because their credential is in use
                  by a published bundle. Forcing the delete will degrade those bundles
                  back to "user provides". This action cannot be undone.
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
                  {labelFor(b.user_id)}
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
