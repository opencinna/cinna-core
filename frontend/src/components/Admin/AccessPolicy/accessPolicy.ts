import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { ServerConfigService, type ServerConfigUpdate } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import {
  accessPolicyReasonCopy,
  parseAccessPolicyReason,
} from "@/utils/accessPolicyReasons"

/**
 * What the three Access-tab policy cards share.
 *
 * They are three cards about three concerns, but they read one row and write
 * one endpoint, and they describe each other in their own header sentences —
 * the Registration card's summary is derived from the same fields the Sign-in
 * card toggles. Every one of those is a place two cards can drift apart, so
 * the query, the mutation and the sentences live here once.
 */

export const REGISTRATION_MODE_OPEN = "open"
export const REGISTRATION_MODE_INVITE_ONLY = "invite_only"

/**
 * Copy for a refused save. The reason codes and their prose live in
 * `@/utils/accessPolicyReasons` because the same codes are raised at signup,
 * at password login and on the Google button; only the fallback — what to say
 * about a code no release of this frontend has heard of — is local, because
 * only here is the reader an administrator looking at a form.
 */
export function reasonMessage(error: unknown): string {
  const known = accessPolicyReasonCopy(error)
  if (known) return known
  const { code } = parseAccessPolicyReason(error)
  return code
    ? `Could not save the access policy (${code}).`
    : "Could not save the access policy."
}

/** The non-empty, whitespace-trimmed entries of a comma-separated glob list. */
export function parsePatterns(raw: string): string[] {
  return raw
    .split(",")
    .map((entry) => entry.trim())
    .filter((entry) => entry.length > 0)
}

/** "No restriction" / "1 pattern" / "4 patterns" — the same words everywhere. */
export function describePatternCount(count: number): string {
  if (count === 0) return "No restriction"
  return `${count} ${count === 1 ? "pattern" : "patterns"}`
}

/** The Registration card's header sentence, from the *persisted* policy. */
export function describeRegistration(args: {
  registrationOpen: boolean
  patternCount: number
}): string {
  if (!args.registrationOpen) return "Only people you invite get an account."
  if (args.patternCount > 0) {
    return `Anyone with a matching address can register (${describePatternCount(args.patternCount)}).`
  }
  return "Anyone can register."
}

/** The Sign-in methods card's header sentence. */
export function describeSignIn(args: {
  passwordAuthEnabled: boolean
  googleAuthEnabled: boolean
}): string {
  if (args.passwordAuthEnabled && args.googleAuthEnabled) {
    return "Employees sign in with Google or a password."
  }
  if (args.passwordAuthEnabled) return "Employees sign in with a password."
  return "Employees sign in with Google only."
}

/**
 * The `server_config` singleton, on the one key every card on this page uses.
 *
 * Several cards asking for it is one request: React Query dedupes on the key,
 * and a write from any of them republishes the row for all of them.
 */
export function useServerConfig() {
  return useQuery({
    queryKey: ["serverConfig"],
    queryFn: () => ServerConfigService.getServerConfig(),
  })
}

/**
 * Which field a partial update is currently writing, or `null`.
 *
 * Lets a card disable only the control the admin just used and leave its
 * neighbour live. It reads the first key of the payload because every write on
 * this page carries exactly one field — an auto-saving control saves itself,
 * never the card.
 */
export function pendingConfigField(mutation: {
  isPending: boolean
  variables?: ServerConfigUpdate
}): string | null {
  return mutation.isPending
    ? (Object.keys(mutation.variables ?? {})[0] ?? null)
    : null
}

/**
 * The single partial-update endpoint, with the cache discipline every caller
 * needs.
 *
 * The endpoint returns the updated row, so it is published *before* the
 * refetch: `isPending` goes false the moment the handler runs, and without
 * this every control would re-render from the stale cache — visibly snapping
 * back to the old value under a success toast — until the invalidation
 * resolves, or forever if that refetch fails.
 *
 * `["accessPolicy"]` is invalidated too: the public projection these cards
 * just changed is what the login and signup pages render from, and a stale
 * copy in the admin's own session would show them the old front door.
 *
 * `onError` is overridable for the one control that has somewhere better than
 * a toast to put its failure — the patterns dialog, whose rejected text is
 * still on screen.
 */
export function useServerConfigUpdate(handlers?: {
  onSaved?: () => void
  onError?: (message: string) => void
}) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  return useMutation({
    mutationFn: (data: ServerConfigUpdate) =>
      ServerConfigService.updateServerConfig({ requestBody: data }),
    onSuccess: (data) => {
      queryClient.setQueryData(["serverConfig"], data)
      queryClient.invalidateQueries({ queryKey: ["serverConfig"] })
      queryClient.invalidateQueries({ queryKey: ["accessPolicy"] })
      showSuccessToast("Access policy updated")
      handlers?.onSaved?.()
    },
    onError: (error) => {
      const message = reasonMessage(error)
      if (handlers?.onError) {
        handlers.onError(message)
        return
      }
      showErrorToast(message)
    },
  })
}
