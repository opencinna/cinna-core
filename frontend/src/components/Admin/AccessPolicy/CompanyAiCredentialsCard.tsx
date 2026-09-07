import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { KeyRound } from "lucide-react"
import { useMemo, useState } from "react"

import {
  AdminLlmProvidersService,
  type ManagedAICredentialPublic,
  type ManagedAICredentialReconcileResult,
} from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import { CompanyAiCredentialRow } from "@/components/Admin/AccessPolicy/CompanyAiCredentialRow"
import {
  type AutoProvisionConflict,
  describeAutoProvisionConflict,
  MANAGED_CREDENTIALS_QUERY_PREFIX,
  managedCredentialsQueryKey,
  parseAutoProvisionConflict,
} from "@/components/Admin/LlmProviders/providerTypes"
import { PREVIEW_COUNT, PreviewList } from "@/components/Common/PreviewList"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { userRoleLabel } from "@/utils/userRoles"

interface ToggleVariables {
  record: ManagedAICredentialPublic
  role: string
  checked: boolean
}

/** A refusal, together with the segment the admin actually clicked. */
interface RefusedToggle {
  detail: AutoProvisionConflict
  recordName: string
  role: string
}

/**
 * Granting first, then alphabetical.
 *
 * The card can only show five rows, so which five is a decision: the ones that
 * actually hand a key to a new account are the answer to the question this
 * card asks, and the rest is what the link at the foot is for.
 */
function sortForPreview(
  records: ManagedAICredentialPublic[],
): ManagedAICredentialPublic[] {
  return [...records].sort((a, b) => {
    const aGrants = (a.auto_provision_roles ?? []).length > 0
    const bGrants = (b.auto_provision_roles ?? []).length > 0
    if (aGrants !== bGrants) return aGrants ? -1 : 1
    return a.name.localeCompare(b.name)
  })
}

/**
 * Which company AI keys a new account of each role receives.
 *
 * The flag itself lives on `ManagedAICredential.auto_provision_roles` and is
 * edited on the AI Credentials page alongside everything else about a
 * credential. This is the same flag seen from the other end: an admin setting
 * up the front door is asking "what does a new Agent Developer get?", and
 * answering that from a list of credentials means opening each one in turn.
 * So the card reads and writes the same field, and never invents state of its
 * own — a click here is one PATCH to one credential.
 *
 * A preview list (P5) at half width, not the full-width table this used to be:
 * a name plus a three-segment role toggle is a list of rows, and the segmented
 * group is one control (guidelines §2 "Card width" / "Toggles on rows"). The
 * table that would genuinely need its columns would also need search, which
 * makes it the `/admin/ai-credentials` route the footer link already goes to.
 */
export function CompanyAiCredentialsCard() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // The 409 the server raises when two credentials would own the same
  // (role, mode) default. Held with the segment that caused it so the message
  // can sit under the list that is still on screen — and so it can name that
  // segment: a refusal that mentions only the *other* credential leaves an
  // admin who clicked twice in quick succession guessing which one bounced,
  // and the segment itself is back to unticked either way.
  const [conflict, setConflict] = useState<RefusedToggle | null>(null)

  // The records with a write in flight, so a row can be frozen while its
  // neighbours stay live.
  //
  // Not `toggleMutation.isPending && variables.record.id === record.id`: all
  // rows share one mutation observer, and `mutate()` detaches it from the
  // previous call, so `isPending` and `variables` describe only the *latest*
  // click. Clicking a second row would silently thaw the first while its PATCH
  // was still out — and a second click on that row would then compute its next
  // role list from a cache the first response had not written yet, quietly
  // undoing the first change. That is exactly the window `onSuccess` below
  // closes, so the freeze has to be per record, and counted here.
  //
  // What makes the set *sufficient* and not merely accurate: React Query
  // awaits `onSuccess` before `onSettled`, so the cache write-through strictly
  // precedes the release below and there is no instant in which the row is
  // live and the cache is behind. Moving that `setQueryData` into `onSettled`
  // would look like tidying and would silently reopen the window.
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(new Set())

  const releasePending = (recordId: string) =>
    setPendingIds((ids) => {
      const next = new Set(ids)
      next.delete(recordId)
      return next
    })

  const {
    data: records,
    isError,
    error,
    refetch,
  } = useQuery({
    // The same key the AI Credentials page uses, so a change made there is
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
    onMutate: ({ record }) => {
      setConflict(null)
      setPendingIds((ids) => new Set(ids).add(record.id))
    },
    onSuccess: (result, { checked }) => {
      // The authoritative post-write row, straight into the cache the next
      // click reads its `auto_provision_roles` from.
      //
      // This used to be discarded and the cache refreshed by a
      // fire-and-forget `invalidateQueries` in `onSettled`. The promise was
      // not returned, so `isPending` went false — and the controls came back
      // to life — a refetch round-trip before the cache held the new roles.
      // Granting a second role inside that window computed `next` from the
      // pre-PATCH list and sent it *without* the role just granted, quietly
      // undoing it, with two green toasts and no error. Writing the row here
      // closes the window rather than narrowing it: there is no interval in
      // which the control is live and the cache is behind.
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
      // Released here as well as in `onSettled`, which is skipped when this
      // handler throws — and an id stranded in the set freezes that row until
      // a reload. Safe to unfreeze this early precisely because nothing was
      // written: the cache still holds the list the next click should read.
      // The delete is idempotent, so the `onSettled` release stays the rule.
      releasePending(record.id)
      const detected = parseAutoProvisionConflict(error)
      if (detected) {
        setConflict({ detail: detected, recordName: record.name, role })
        return
      }
      handleError.call(showErrorToast, error)
    },
    onSettled: (_result, _error, { record }) => {
      releasePending(record.id)
      // Still invalidated — the write above keeps this row honest, but a
      // conflict is evidence that *another* row is involved and the whole list
      // may have moved under a different admin.
      void queryClient.invalidateQueries({
        queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX,
      })
    },
  })

  const rows = useMemo(() => sortForPreview(records ?? []), [records])

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <KeyRound className="h-5 w-5" />
          Company AI credentials
        </CardTitle>
        <CardDescription>
          Granted when an account is created. Changing a role later never grants
          or revokes a key — use "Apply to existing users" on the AI Credentials
          page for accounts that already exist.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <PreviewList
          items={rows}
          getKey={(record) => record.id}
          renderItem={(record) => (
            <CompanyAiCredentialRow
              record={record}
              records={rows}
              isPending={pendingIds.has(record.id)}
              onToggle={(role, checked) =>
                toggleMutation.mutate({ record, role, checked })
              }
            />
          )}
          isLoading={records === undefined}
          // Only when the failure left nothing to show: a background refetch
          // that fails must not replace a live list with an error panel.
          isError={isError && records === undefined}
          error={error}
          onRetry={() => refetch()}
          errorFallback="Couldn't read the managed AI credentials."
          empty={
            <p className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
              No managed AI credentials yet —{" "}
              <Link
                to="/admin/ai-credentials"
                className="text-primary hover:underline"
              >
                create one
              </Link>{" "}
              to hand new accounts a working key on day one.
            </p>
          }
          // Not passed: this card's Show-all is a `Link` with a two-way label
          // (below), and `PreviewList` deliberately leaves the destination to
          // the consumer.
        />

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

        {/* One link, not two. The tail of the list and "edit these properly"
            are the same destination, so a Show-all link beside a Manage link
            would be two routes to one page. Withheld while the list is empty:
            the empty state carries its own link to the same page. */}
        {rows.length > 0 && (
          <div>
            <Button asChild variant="link" className="h-auto px-0">
              <Link to="/admin/ai-credentials">
                {rows.length > PREVIEW_COUNT
                  ? `Show all (${rows.length}) on AI Credentials`
                  : "Manage AI credentials"}
              </Link>
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
