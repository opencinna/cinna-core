import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Ban, Link2, Send } from "lucide-react"

import { type UserPublic, UsersService } from "@/client"
import {
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import {
  formatDaysUntil,
  INVITATION_STATUS_PENDING,
  INVITATION_STATUS_REVOKED,
  isOutstandingInvitation,
  knownInvitationStatus,
} from "@/utils/invitationStatus"

interface InvitationActionsProps {
  user: UserPublic
  onSuccess: () => void
}

/**
 * The shape `POST /users/{id}/invitation/resend` returns on a 429.
 *
 * The route answers a per-row cooldown with a structured `detail`
 * (`{code, message, resend_available_at}`) plus a `Retry-After` header. The
 * generated client throws an `ApiError` carrying only the body, so the
 * deadline is read from `resend_available_at` — the header is not reachable
 * here and does not need to be.
 */
interface ResendCooldown {
  message: string
  availableAt: string | null
}

function parseResendCooldown(error: unknown): ResendCooldown | null {
  const detail = (error as { body?: { detail?: unknown } })?.body?.detail
  if (!detail || typeof detail !== "object") return null
  const record = detail as Record<string, unknown>
  if (record.code !== "invitation_resend_cooldown") return null
  return {
    message:
      typeof record.message === "string"
        ? record.message
        : "This invitation was sent recently.",
    availableAt:
      typeof record.resend_available_at === "string"
        ? record.resend_available_at
        : null,
  }
}

/** "Try again after 14:32." — local time, so the admin can just wait it out. */
function cooldownCopy(cooldown: ResendCooldown): string {
  if (!cooldown.availableAt) return cooldown.message
  const when = new Date(cooldown.availableAt)
  if (Number.isNaN(when.getTime())) return cooldown.message
  return `${cooldown.message} You can send another after ${when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.`
}

/**
 * Invitation lifecycle entries for the users-table row menu.
 *
 * Rendered for every non-accepted invitation — `pending`, `expired` and
 * `revoked`. An accepted one has nothing left to offer, and a status this
 * build does not recognise gets no actions rather than a guess (see
 * `invitationStatus.ts`).
 *
 * The expiry line needs `expires_at`, which `UserPublic` does not carry, so it
 * is fetched here rather than on every row: `DropdownMenuContent` unmounts
 * when the menu closes, so this component only exists — and the query only
 * runs — while one row's menu is open.
 */
export function InvitationActions({ user, onSuccess }: InvitationActionsProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const status = knownInvitationStatus(user.invitation_status)
  const isPending = status === INVITATION_STATUS_PENDING
  const isRevoked = status === INVITATION_STATUS_REVOKED
  const outstanding = isOutstandingInvitation(user.invitation_status)

  const { data: invitation } = useQuery({
    queryKey: ["users", user.id, "invitation"],
    queryFn: () => UsersService.readUserInvitation({ userId: user.id }),
    // The endpoint 404s for an account that was never invited, so it is only
    // asked when the row already says there is something to read.
    enabled: outstanding,
    retry: false,
    staleTime: 30_000,
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["users"] })
  }

  const resendMutation = useMutation({
    mutationFn: () => UsersService.resendUserInvitation({ userId: user.id }),
    onSuccess: (result) => {
      showSuccessToast(
        result.email_sent
          ? `A fresh invitation link was emailed to ${user.email}.`
          : "A fresh link was issued, but no email was sent — this instance has no outbound mail configured. Use “Copy invite link”.",
      )
      onSuccess()
    },
    onError: (error) => {
      const cooldown = parseResendCooldown(error)
      showErrorToast(
        cooldown
          ? cooldownCopy(cooldown)
          : getErrorMessage(error, "Could not resend the invitation."),
      )
    },
    onSettled: invalidate,
  })

  const copyLinkMutation = useMutation({
    // Deliberately the *link* endpoint, not resend: reading the outstanding
    // link out must not rotate `token_jti`, or an admin copying a link for
    // chat would silently kill the email the person is about to click.
    mutationFn: () => UsersService.readUserInvitationLink({ userId: user.id }),
    onSuccess: async (result) => {
      try {
        await navigator.clipboard.writeText(result.accept_url)
        showSuccessToast(
          `Invitation link copied. It signs the holder in as ${user.email} — share it privately.`,
        )
      } catch {
        // Clipboard access can be refused (insecure origin, permissions).
        // Surfacing the URL is more useful than a bare failure.
        showErrorToast(
          `Could not copy automatically. Link: ${result.accept_url}`,
        )
      }
      onSuccess()
    },
    onError: (error) => {
      showErrorToast(
        getErrorMessage(
          error,
          "No live link for this invitation. Resend it to issue a new one.",
        ),
      )
    },
  })

  const revokeMutation = useMutation({
    mutationFn: () => UsersService.revokeUserInvitation({ userId: user.id }),
    onSuccess: () => {
      showSuccessToast(
        `The invitation for ${user.email} was revoked. Its link no longer works.`,
      )
      onSuccess()
    },
    onError: (error) => {
      showErrorToast(getErrorMessage(error, "Could not revoke the invitation."))
    },
    onSettled: invalidate,
  })

  if (!outstanding) return null

  const expiry = formatDaysUntil(invitation?.expires_at)

  return (
    <>
      <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">
        {isRevoked
          ? "Invitation revoked"
          : expiry
            ? isPending
              ? `Invitation expires ${expiry}`
              : `Expired ${expiry}`
            : "Invitation outstanding"}
      </DropdownMenuLabel>

      <DropdownMenuItem
        onSelect={(e) => e.preventDefault()}
        disabled={resendMutation.isPending}
        onClick={() => resendMutation.mutate()}
      >
        <Send />
        {/* Resend clears `revoked_at` as well as rotating the token, so from a
            revoked row this really is a new offer rather than another copy of
            one already outstanding — and it is the only way back. */}
        {isRevoked ? "Send new invitation" : "Resend invitation"}
      </DropdownMenuItem>

      {isPending && (
        <DropdownMenuItem
          onSelect={(e) => e.preventDefault()}
          disabled={copyLinkMutation.isPending}
          onClick={() => copyLinkMutation.mutate()}
        >
          <Link2 />
          Copy invite link
        </DropdownMenuItem>
      )}

      {!isRevoked && (
        <DropdownMenuItem
          variant="destructive"
          onSelect={(e) => e.preventDefault()}
          disabled={revokeMutation.isPending}
          onClick={() => revokeMutation.mutate()}
        >
          <Ban />
          Revoke invitation
        </DropdownMenuItem>
      )}

      <DropdownMenuSeparator />
    </>
  )
}
