import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Loader2, RotateCw } from "lucide-react"

import {
  AdminLlmProvidersService,
  type ManagedAICredentialMember,
  type ManagedAICredentialPublic,
  type MembershipProvisioningStatus,
} from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import {
  describeProvisionError,
  MEMBERSHIP_STATUS_META,
  membershipStatusMeta,
} from "@/utils/keyProvisioning"
import { MANAGED_CREDENTIALS_QUERY_PREFIX } from "./providerTypes"

const TONE_CLASS: Record<
  (typeof MEMBERSHIP_STATUS_META)[MembershipProvisioningStatus]["tone"],
  string
> = {
  neutral: "text-muted-foreground",
  progress: "text-sky-600 dark:text-sky-400",
  ok: "text-emerald-600 dark:text-emerald-400",
  error: "text-destructive",
}

/** Does any member of this record still have a key on the way? */
export function hasKeyInFlight(record: ManagedAICredentialPublic): boolean {
  return (record.members ?? []).some(
    (member) => membershipStatusMeta(member.provisioning_status).inFlight,
  )
}

/**
 * Where this record's keys stand, in one cell.
 *
 * Counting is presentation: every status counted here was stated by the server
 * on the member row. Nothing is inferred from which other fields are null.
 */
export function KeyStatusSummary({
  record,
}: {
  record: ManagedAICredentialPublic
}) {
  const members = record.members ?? []
  if (record.provisioning_mode !== "minted") {
    return (
      <Badge variant="outline" className="text-muted-foreground">
        Shared key
      </Badge>
    )
  }
  if (members.length === 0) {
    return <span className="text-xs text-muted-foreground">No members</span>
  }

  const counts = new Map<MembershipProvisioningStatus, number>()
  for (const member of members) {
    const status = member.provisioning_status
    counts.set(status, (counts.get(status) ?? 0) + 1)
  }

  return (
    <div className="flex flex-wrap gap-1">
      {[...counts.entries()].map(([status, count]) => {
        const meta = membershipStatusMeta(status)
        return (
          <span
            key={status}
            className={`inline-flex items-center gap-1 whitespace-nowrap text-xs ${TONE_CLASS[meta.tone]}`}
          >
            {meta.inFlight && <Loader2 className="size-3 animate-spin" />}
            {count} {meta.adminLabel.toLowerCase()}
          </span>
        )
      })}
    </div>
  )
}

/**
 * One member, with the state of their key.
 *
 * `failed` is terminal by design — bounded retries that converge are what makes
 * the status mean "somebody has to look at this" — so it carries both the
 * reason and the way out.
 */
export function MemberChip({
  record,
  member,
}: {
  record: ManagedAICredentialPublic
  member: ManagedAICredentialMember
}) {
  const status = member.provisioning_status
  const meta = membershipStatusMeta(status)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const retryMutation = useMutation({
    mutationFn: () =>
      AdminLlmProvidersService.retryMemberKeyProvisioning({
        managedCredentialId: record.id,
        userId: member.user_id,
      }),
    onSuccess: () => showSuccessToast("Key creation queued again."),
    onError: (error: ApiError) => handleError.call(showErrorToast, error),
    onSettled: () => {
      void queryClient.invalidateQueries({
        queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX,
      })
    },
  })

  const label = member.full_name
    ? `${member.full_name} <${member.email}>`
    : member.email

  return (
    <div className="inline-flex max-w-full items-center gap-1.5 rounded-full border bg-muted/40 px-3 py-0.5 text-xs">
      <span className="truncate">{label}</span>
      {status !== "not_applicable" && (
        <span className={`inline-flex items-center gap-1 ${TONE_CLASS[meta.tone]}`}>
          {meta.inFlight && <Loader2 className="size-3 animate-spin" />}
          {meta.adminLabel}
        </span>
      )}
      {status === "failed" && (
        <>
          <span
            className="max-w-[16rem] truncate text-muted-foreground"
            title={describeProvisionError(member.provision_error)}
          >
            {describeProvisionError(member.provision_error)}
          </span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-5 px-1 text-xs"
            disabled={retryMutation.isPending}
            onClick={() => retryMutation.mutate()}
          >
            <RotateCw className="mr-1 size-3" />
            Retry
          </Button>
        </>
      )}
    </div>
  )
}
