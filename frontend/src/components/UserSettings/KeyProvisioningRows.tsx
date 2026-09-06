import { useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertCircle, Loader2 } from "lucide-react"
import { useEffect, useRef } from "react"

import { AiCredentialsService, type UserKeyProvisioningPublic } from "@/client"
import {
  membershipStatusMeta,
  MY_KEY_PROVISIONINGS_QUERY_KEY,
} from "@/utils/keyProvisioning"

/**
 * Keys an administrator is having created for this user.
 *
 * A separate list from the credentials above it, because it is a separate
 * thing: every credential in that list is usable, and these are memberships
 * with no key behind them yet. The server decides which state each one is in
 * and this renders it — nothing here reconstructs a status, and nothing here
 * offers Use or Default, because there is no key to use or to default to.
 */
export function useMyKeyProvisionings() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: MY_KEY_PROVISIONINGS_QUERY_KEY,
    queryFn: () => AiCredentialsService.listMyKeyProvisionings(),
    // Follow a key that is still being created, and stop when it settles. A
    // `failed` row is terminal, so it does not keep the page spinning.
    refetchInterval: (data) =>
      (data.state.data ?? []).some(
        (row) => membershipStatusMeta(row.status).inFlight,
      )
        ? 10_000
        : false,
  })

  const inFlightCount = (query.data ?? []).filter(
    (row) => membershipStatusMeta(row.status).inFlight,
  ).length
  const previousInFlight = useRef(inFlightCount)

  // A key that lands leaves this list and appears in the credentials list — a
  // different query, which has no reason to refetch on its own.
  //
  // **This lives in the hook, not in the row component**, because the row
  // component is rendered by the `else` branch of a ternary whose condition
  // this transition flips: the last in-flight row disappearing is exactly the
  // render that unmounts it, so an effect down there never fires for the case
  // that matters — a brand-new account whose only credential is the one being
  // minted. The thing that notices a transition has to outlive the branch the
  // transition changes.
  useEffect(() => {
    if (inFlightCount < previousInFlight.current) {
      void queryClient.invalidateQueries({ queryKey: ["aiCredentialsList"] })
      void queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
    }
    previousInFlight.current = inFlightCount
  }, [inFlightCount, queryClient])

  return query
}

export function KeyProvisioningRows({
  rows,
}: {
  rows: UserKeyProvisioningPublic[]
}) {
  if (rows.length === 0) return null

  return (
    <>
      {rows.map((row) => {
        const meta = membershipStatusMeta(row.status)
        // Two independent facts, and they used to be one. "Is it still moving"
        // is `meta.inFlight` — the table's own answer, and the same one the
        // polling above asks — not `!failed`, which made this a second opinion
        // that disagrees the moment a status is neither. "Did it fail" is what
        // picks the copy. A row that is neither gets the neutral spinner-free
        // rendering and its own label rather than a spinner that never stops.
        const failed = row.status === "failed"
        return (
          <div
            key={row.managed_credential_id}
            className="flex items-start justify-between gap-3 rounded-lg border border-dashed px-3 py-2"
          >
            <div className="min-w-0 space-y-0.5">
              <div className="flex items-center gap-2">
                <span className="truncate text-sm font-medium text-muted-foreground">
                  {row.name}
                </span>
              </div>
              <p
                className={`flex items-center gap-1.5 text-xs ${
                  meta.inFlight
                    ? "text-sky-600 dark:text-sky-400"
                    : "text-destructive"
                }`}
              >
                {meta.inFlight ? (
                  <Loader2 className="size-3.5 shrink-0 animate-spin" />
                ) : (
                  <AlertCircle className="size-3.5 shrink-0" />
                )}
                {failed
                  ? "This key could not be set up. Ask your administrator to look at it — they can start it again from the AI Credentials admin page."
                  : meta.inFlight
                    ? "Your administrator is setting this key up for you. It appears here as a credential once it is ready."
                    : "This key is not ready yet. Ask your administrator to look at it."}
              </p>
            </div>
            <span className="shrink-0 text-xs text-muted-foreground">
              {meta.ownerLabel}
            </span>
          </div>
        )
      })}
    </>
  )
}
