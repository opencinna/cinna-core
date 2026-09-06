import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { ExternalLink, Loader2 } from "lucide-react"
import { useState } from "react"

import {
  type ManagedAICredentialPublic,
  type ManagedAICredentialReconcileResult,
  AdminLlmProvidersService,
} from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import {
  type AutoProvisionConflict,
  describeAutoProvisionConflict,
  findAutoProvisionConflict,
  getProviderTypeLabel,
  MANAGED_CREDENTIALS_QUERY_PREFIX,
  managedCredentialsQueryKey,
  parseAutoProvisionConflict,
} from "@/components/Admin/LlmProviders/providerTypes"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { USER_ROLE_OPTIONS, userRoleLabel } from "@/utils/userRoles"

interface ToggleVariables {
  record: ManagedAICredentialPublic
  role: string
  checked: boolean
}

/** A refusal, together with the cell the admin actually clicked. */
interface RefusedToggle {
  detail: AutoProvisionConflict
  recordName: string
  role: string
}

function cellKey(recordId: string, role: string): string {
  return `${recordId}:${role}`
}

/**
 * Which company AI keys a new account receives, as credentials × roles.
 *
 * The flag itself lives on `ManagedAICredential.auto_provision_roles` and is
 * edited on the LLM Providers page alongside everything else about a
 * credential. This is the same flag seen from the other end: an admin setting
 * up the front door is asking "what does a new Agent Developer get?", and
 * answering that from a list of credentials means opening each one in turn.
 * So the matrix reads and writes the same field, and never invents state of
 * its own — a toggle here is one PATCH to one credential.
 */
export function AutoProvisionedCredentialsMatrix() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // The 409 the server raises when two credentials would own the same
  // (role, mode) default. Held with the cell that caused it so the message can
  // sit under the table that is still on screen — and so it can name that
  // cell: a refusal that mentions only the *other* credential leaves an admin
  // who clicked two cells in quick succession guessing which one bounced, and
  // the checkbox itself is back to unticked either way.
  const [conflict, setConflict] = useState<RefusedToggle | null>(null)

  const {
    data: records,
    isLoading,
    isError,
  } = useQuery({
    // The same key the LLM Providers page uses, so a change made there is
    // already reflected here (and vice versa) without a second source of
    // truth.
    queryKey: managedCredentialsQueryKey(),
    queryFn: () => AdminLlmProvidersService.listManagedAiCredentials({}),
    staleTime: 30_000,
  })

  const toggleMutation = useMutation<
    ManagedAICredentialReconcileResult,
    ApiError,
    ToggleVariables
  >({
    mutationFn: ({ record, role, checked }) => {
      const current = record.auto_provision_roles ?? []
      const next = checked
        ? [...current, role]
        : current.filter((entry) => entry !== role)
      return AdminLlmProvidersService.updateManagedAiCredential({
        managedCredentialId: record.id,
        // Only this field. Every other property is `null`/omitted, which the
        // backend reads as "unchanged" — membership included.
        requestBody: { auto_provision_roles: next },
      })
    },
    onMutate: () => setConflict(null),
    onSuccess: (result, { checked }) => {
      // The authoritative post-write row, straight into the cache the next
      // toggle reads its `auto_provision_roles` from.
      //
      // This used to be discarded and the cache refreshed by a
      // fire-and-forget `invalidateQueries` in `onSettled`. The promise was
      // not returned, so `isPending` went false — and the checkboxes came back
      // to life — a refetch round-trip before the cache held the new roles.
      // Ticking a second role inside that window computed `next` from the
      // pre-PATCH list and sent it *without* the role just granted, quietly
      // undoing it, with two green toasts and no error. Writing the row here
      // closes the window rather than narrowing it: there is no interval in
      // which the controls are live and the cache is behind.
      queryClient.setQueryData<ManagedAICredentialPublic[]>(
        managedCredentialsQueryKey(),
        (rows) =>
          rows?.map((row) =>
            row.id === result.record.id ? result.record : row,
          ),
      )
      showSuccessToast(
        checked
          ? "New accounts with that role will receive this credential."
          : "New accounts with that role will no longer receive this credential.",
      )
    },
    onError: (error, { record, role }) => {
      const detected = parseAutoProvisionConflict(error)
      if (detected) {
        setConflict({ detail: detected, recordName: record.name, role })
        return
      }
      handleError.call(showErrorToast, error)
    },
    onSettled: () => {
      // Still invalidated — the write above keeps this row honest, but a
      // conflict is evidence that *another* row is involved and the whole list
      // may have moved under a different admin.
      void queryClient.invalidateQueries({
        queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX,
      })
    },
  })

  const pendingCell =
    toggleMutation.isPending && toggleMutation.variables
      ? cellKey(
          toggleMutation.variables.record.id,
          toggleMutation.variables.role,
        )
      : null

  const header = (
    <div className="flex items-baseline justify-between gap-4">
      <div className="min-w-0">
        <Label className="text-sm font-medium">Company AI credentials</Label>
        <p className="text-xs text-muted-foreground">
          Granted when an account is created. Changing a role later never grants
          or revokes a key — use "Apply to existing users" on the LLM Providers
          page for accounts that already exist.
        </p>
      </div>
      <Link
        to="/admin/llm-providers"
        className="inline-flex shrink-0 items-center gap-1 text-xs text-muted-foreground hover:text-foreground hover:underline"
      >
        Manage AI credentials
        <ExternalLink className="h-3 w-3" />
      </Link>
    </div>
  )

  if (isLoading) {
    return (
      <div className="space-y-2">
        {header}
        <Skeleton className="h-24 w-full" />
      </div>
    )
  }

  if (isError) {
    return (
      <div className="space-y-2">
        {header}
        <p className="text-xs text-destructive">
          Couldn't read the managed AI credentials.
        </p>
      </div>
    )
  }

  const rows = records ?? []

  if (rows.length === 0) {
    return (
      <div className="space-y-2">
        {header}
        <p className="rounded-md border border-dashed px-3 py-6 text-center text-xs text-muted-foreground">
          No managed AI credentials yet —{" "}
          <Link
            to="/admin/llm-providers"
            className="text-primary hover:underline"
          >
            create one
          </Link>{" "}
          to hand new accounts a working key on day one.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {header}

      <div className="overflow-x-auto rounded-md border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/40">
              <th className="px-3 py-2 text-left font-medium">Credential</th>
              {USER_ROLE_OPTIONS.map((role) => (
                <th
                  key={role.value}
                  className="whitespace-nowrap px-3 py-2 text-center font-medium"
                >
                  {role.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((record) => {
              const roles = record.auto_provision_roles ?? []
              return (
                <tr key={record.id} className="border-b last:border-b-0">
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{record.name}</span>
                      <Badge variant="outline" className="font-normal">
                        {getProviderTypeLabel(record.type)}
                      </Badge>
                    </div>
                  </td>
                  {USER_ROLE_OPTIONS.map((role) => {
                    const checked = roles.includes(role.value)
                    // Advisory only. Computed from the list already loaded, so
                    // it can be stale; the click still goes to the server and
                    // a real refusal is rendered below the table.
                    const clash = checked
                      ? null
                      : findAutoProvisionConflict(rows, record, role.value)
                    const key = cellKey(record.id, role.value)
                    const control = (
                      <Checkbox
                        id={key}
                        checked={checked}
                        // Including the cell in flight. It is controlled off
                        // the not-yet-refetched list, so it does not visibly
                        // move after the first click; leaving it live invites a
                        // second click, and a second PATCH is a second
                        // full reconcile and a second audit event for one
                        // intended change.
                        disabled={toggleMutation.isPending}
                        aria-label={`Auto-provision ${record.name} for ${role.label} accounts`}
                        onCheckedChange={(next) =>
                          toggleMutation.mutate({
                            record,
                            role: role.value,
                            checked: next === true,
                          })
                        }
                      />
                    )
                    return (
                      <td key={role.value} className="px-3 py-2">
                        <div className="flex items-center justify-center gap-1.5">
                          {pendingCell === key ? (
                            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                          ) : (
                            control
                          )}
                          {clash && (
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Badge
                                  variant="outline"
                                  className="cursor-help border-amber-500/50 text-[10px] font-normal text-amber-600 dark:text-amber-500"
                                >
                                  Conflict
                                </Badge>
                              </TooltipTrigger>
                              <TooltipContent className="max-w-xs">
                                "{clash.name}" already sets this role's default
                                for a mode "{record.name}" also wires. Ticking
                                this will be refused.
                              </TooltipContent>
                            </Tooltip>
                          )}
                        </div>
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {conflict && (
        <Alert variant="destructive">
          <AlertTitle>Another credential owns that default</AlertTitle>
          <AlertDescription>
            {userRoleLabel(conflict.role)} accounts couldn't be added to "
            {conflict.recordName}".{" "}
            {describeAutoProvisionConflict(conflict.detail)}
          </AlertDescription>
        </Alert>
      )}
    </div>
  )
}
