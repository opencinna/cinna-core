import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  BadgeCheck,
  EllipsisVertical,
  Pencil,
  ShieldCheck,
  Trash,
} from "lucide-react"
import { useState } from "react"

import {
  AdminProviderCredentialsService,
  type ProviderAdminCredentialPublic,
  type ProviderAdminCredentialVerifyResult,
} from "@/client"
import { ApiError } from "@/client/core/ApiError"
import { PROVIDER_ADMIN_CREDENTIALS_QUERY_KEY } from "@/components/Admin/LlmProviders/providerTypes"
import { useProviderAdapters } from "@/components/Admin/LlmProviders/useProviderAdapters"
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
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { ProviderAdminCredentialDialog } from "./ProviderAdminCredentialDialog"

interface ProviderAdminCredentialsTableProps {
  records: ProviderAdminCredentialPublic[]
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "Never"
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })
}

function describeVerifyResult(result: ProviderAdminCredentialVerifyResult): string {
  if (!result.ok) {
    return `Verification failed${result.error ? ` — ${result.error}` : ""}.`
  }
  if (!result.spend_limit_enforcing) {
    // Actionable, because this is no longer something Cinna can fix from here:
    // the limit lives on the provider's console and nowhere else.
    return "The key works, but the project has no monthly spend limit. Set one in the OpenAI console — keys are not created in an uncapped project."
  }
  return "The key works and the project has a monthly spend limit."
}

export function ProviderAdminCredentialsTable({
  records,
}: ProviderAdminCredentialsTableProps) {
  const { adapterFor } = useProviderAdapters()

  if (records.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 rounded-md border border-dashed py-20 text-center">
        <p className="text-muted-foreground">No provider organisations connected.</p>
        <p className="max-w-md text-xs text-muted-foreground">
          Connect one to create a separate API key for each person. For a
          provider whose administration API does not create keys, a key is
          pasted for each person on the Managed credentials tab.
        </p>
      </div>
    )
  }

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-[26%]">Name</TableHead>
            <TableHead className="w-[14%]">Provider</TableHead>
            <TableHead className="w-[20%]">Project</TableHead>
            <TableHead className="w-[16%]">Verified</TableHead>
            <TableHead>Last error</TableHead>
            <TableHead className="w-[48px]" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {records.map((record) => (
            <TableRow key={record.id} className="align-top">
              <TableCell className="font-medium">
                <div>{record.name}</div>
                <div className="text-xs text-muted-foreground">
                  {record.minting_credential_count ?? 0} managed{" "}
                  {(record.minting_credential_count ?? 0) === 1
                    ? "credential mints"
                    : "credentials mint"}{" "}
                  through it · {record.live_minted_key_count ?? 0} live{" "}
                  {(record.live_minted_key_count ?? 0) === 1 ? "key" : "keys"}
                </div>
              </TableCell>
              <TableCell>
                <Badge variant="secondary">
                  {adapterFor(record.provider_type)?.label ?? record.provider_type}
                </Badge>
              </TableCell>
              <TableCell className="font-mono text-xs">
                {record.config.project_id || "—"}
              </TableCell>
              <TableCell className="text-sm text-muted-foreground">
                {formatDateTime(record.last_verified_at)}
              </TableCell>
              <TableCell className="text-sm">
                {record.last_verify_error ? (
                  <span className="text-destructive">{record.last_verify_error}</span>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </TableCell>
              <TableCell className="text-right">
                <ProviderAdminCredentialActions record={record} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

function ProviderAdminCredentialActions({
  record,
}: {
  record: ProviderAdminCredentialPublic
}) {
  const [isEditOpen, setIsEditOpen] = useState(false)
  const [isDeleteOpen, setIsDeleteOpen] = useState(false)
  // Set once a non-forced delete has been refused, so the second press is a
  // deliberate override rather than a retry of the same request.
  const [refusal, setRefusal] = useState<string | null>(null)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const invalidate = () => {
    void queryClient.invalidateQueries({
      queryKey: PROVIDER_ADMIN_CREDENTIALS_QUERY_KEY,
    })
  }

  const verifyMutation = useMutation({
    mutationFn: () =>
      AdminProviderCredentialsService.verifyProviderAdminCredential({
        credentialId: record.id,
      }),
    onSuccess: (result) => {
      const message = describeVerifyResult(result)
      if (result.ok && result.spend_limit_enforcing) showSuccessToast(message)
      else showErrorToast(message)
    },
    onError: (error: ApiError) => handleError.call(showErrorToast, error),
    onSettled: invalidate,
  })

  const deleteMutation = useMutation({
    mutationFn: (force: boolean) =>
      AdminProviderCredentialsService.deleteProviderAdminCredential({
        credentialId: record.id,
        force,
      }),
    onSuccess: () => {
      showSuccessToast("Provider organisation disconnected.")
      setIsDeleteOpen(false)
      setRefusal(null)
    },
    onError: (error: ApiError) => {
      const detail = refusalMessage(error)
      if (detail) {
        setRefusal(detail)
        return
      }
      handleError.call(showErrorToast, error)
    },
    onSettled: invalidate,
  })

  // The server's answer, not a count the browser re-thresholds. `delete_blocked`
  // is the rule; the two counts beside it are the explanation.
  const blocked = record.delete_blocked === true

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon" aria-label="Provider key actions">
            <EllipsisVertical className="h-4 w-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            onClick={() => verifyMutation.mutate()}
            disabled={verifyMutation.isPending}
          >
            <BadgeCheck className="mr-2 h-4 w-4" />
            Verify
          </DropdownMenuItem>
          <DropdownMenuItem onClick={() => setIsEditOpen(true)}>
            <Pencil className="mr-2 h-4 w-4" />
            Edit / rotate key
          </DropdownMenuItem>
          <DropdownMenuItem
            variant="destructive"
            onClick={() => {
              setRefusal(null)
              setIsDeleteOpen(true)
            }}
          >
            <Trash className="mr-2 h-4 w-4" />
            Disconnect
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <ProviderAdminCredentialDialog
        mode="edit"
        record={record}
        open={isEditOpen}
        onOpenChange={setIsEditOpen}
      />

      <AlertDialog
        open={isDeleteOpen}
        onOpenChange={(open) => {
          setIsDeleteOpen(open)
          if (!open) setRefusal(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Disconnect "{record.name}"?</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                <p>
                  This key is the only way to destroy the keys created with it.
                  Once it is gone, any key still live at the provider stays live
                  and has to be removed in the provider's own console.
                </p>
                {blocked && (
                  <p className="flex items-start gap-2 text-amber-600 dark:text-amber-500">
                    <ShieldCheck className="mt-0.5 size-4 shrink-0" />
                    <span>
                      {record.minting_credential_count ?? 0} managed{" "}
                      {(record.minting_credential_count ?? 0) === 1
                        ? "credential still mints"
                        : "credentials still mint"}{" "}
                      through it, and {record.live_minted_key_count ?? 0} minted{" "}
                      {(record.live_minted_key_count ?? 0) === 1 ? "key is" : "keys are"}{" "}
                      still live.
                    </span>
                  </p>
                )}
                {refusal && <p className="text-destructive">{refusal}</p>}
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(event) => {
                // Kept open on the first press so a refusal has somewhere to
                // render; the second press is the explicit override.
                event.preventDefault()
                deleteMutation.mutate(refusal !== null)
              }}
              disabled={deleteMutation.isPending}
            >
              {refusal !== null ? "Disconnect anyway" : "Disconnect"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

/**
 * The 409 the delete gate raises, as a sentence. `null` for anything else, so
 * an unrelated failure still reaches the generic error toast rather than being
 * silently rendered as a refusal the admin can override.
 */
function refusalMessage(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const detail = (error.body as { detail?: unknown } | undefined)?.detail
  if (!detail || typeof detail !== "object") return null
  const body = detail as Record<string, unknown>
  if (body.code !== "provider_admin_credential_in_use") return null
  return typeof body.message === "string"
    ? body.message
    : "This provider key is still in use."
}
