/**
 * People who shared with you — Settings → Channels.
 *
 * The consuming side of identity sharing: who has shared an agent with this
 * user, and whether this user may address them by name.
 *
 * WHY THIS IS ITS OWN CARD AND NOT A SECTION OF A CHANNEL
 * ------------------------------------------------------
 * `IdentityBindingAssignment.is_enabled` is **person-level**. It governs every
 * surface identity reaches — App MCP included — so a copy of it inside a
 * channel row would present a control whose scope is wider than the row it
 * sits on. It is also not a facet of "which chat apps may reach me": "whom I
 * may address" is a different question, and a different card.
 *
 * ONE IDENTITY TOGGLE, NOT TWO
 * ----------------------------
 * These switches are the same person-level toggle Identity MCP uses, read and
 * written through `/users/me/identity-contacts/` — deliberately reused rather
 * than duplicated per channel. A per-channel identity allowlist would be a
 * second source of truth for "may I address this person's identity", and the
 * two would drift.
 *
 * The `["identity-contacts"]` key stays single: the Configure Sheet reads it
 * for its "on but inert" warning, this card reads and writes it. Two keys for
 * one list would let one surface show a person the other has just switched
 * off.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useMemo, useState } from "react"

import { IdentityContactsService } from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { AllIdentityContactsSheet } from "./AllIdentityContactsSheet"
import { IDENTITY_CONTACTS_KEY, sortContacts } from "./channelScopes"
import {
  IdentityConsentFootnote,
  IdentityContactRow,
} from "./IdentityContactRow"

/** Rows shown in the card; the rest live behind "Show all (N)". */
const PREVIEW_COUNT = 5

export function IdentityContactsCard() {
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const [isAllOpen, setIsAllOpen] = useState(false)

  const {
    data: identityContacts,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: IDENTITY_CONTACTS_KEY,
    queryFn: () => IdentityContactsService.listIdentityContacts(),
  })

  const contacts = useMemo(
    () => sortContacts(identityContacts ?? []),
    [identityContacts],
  )
  const totalCount = contacts.length

  // Which per-person toggles have a request in flight, tracked here rather
  // than read off `toggleMutation.variables`. React Query keeps `variables`
  // for the LATEST call only, so two quick toggles on different people would
  // move the spinner to the second row and leave the first looking settled
  // while its request was still open — and the first row's switch re-enabled,
  // inviting a third write to race the one already in flight. A set of ids is
  // the honest model: these requests are concurrent.
  const [pendingContactIds, setPendingContactIds] = useState<
    ReadonlySet<string>
  >(new Set())

  const toggleMutation = useMutation({
    mutationFn: ({
      ownerId,
      isEnabled,
    }: {
      ownerId: string
      isEnabled: boolean
    }) =>
      IdentityContactsService.toggleIdentityContact({
        ownerId,
        requestBody: { is_enabled: isEnabled },
      }),
    onMutate: ({ ownerId }) =>
      setPendingContactIds((prev) => new Set(prev).add(ownerId)),
    // `onSettled`, not `onSuccess`/`onError`: a row must come back out of the
    // pending set on every outcome, including a rejected request.
    onSettled: (_data, _error, { ownerId }) =>
      setPendingContactIds((prev) => {
        const next = new Set(prev)
        next.delete(ownerId)
        return next
      }),
    // Invalidated rather than written into the cache: unlike the channel PUT,
    // this endpoint answers with a bare `Message`, so the client has nothing
    // authoritative to write and guessing would be the start of the drift this
    // tab avoids everywhere else.
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: IDENTITY_CONTACTS_KEY }),
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to update this person")),
  })

  const toggle = (ownerId: string, isEnabled: boolean) =>
    toggleMutation.mutate({ ownerId, isEnabled })

  return (
    <Card>
      <CardHeader>
        <CardTitle>People who shared with you</CardTitle>
        <CardDescription>
          People who have shared an agent with you, and whether you may address
          them by name.
        </CardDescription>
      </CardHeader>

      <CardContent>
        {/* A failed request must never render as "nobody has shared with you",
            which is a claim about other people made from a request that did
            not answer. */}
        {isError ? (
          <QueryErrorAlert
            error={error}
            fallback="Couldn't load the people who have shared their identity with you"
            onRetry={() => refetch()}
          >
            <p className="text-xs">
              This is a failed request, not an empty list.
            </p>
          </QueryErrorAlert>
        ) : isLoading ? (
          <div className="space-y-1.5">
            <Skeleton className="h-[52px] w-full rounded-lg" />
            <Skeleton className="h-[52px] w-full rounded-lg" />
            <Skeleton className="h-[52px] w-full rounded-lg" />
          </div>
        ) : totalCount === 0 ? (
          <p className="text-sm text-muted-foreground">
            Nobody has shared their identity with you yet. When someone does,
            they appear here and you choose whether to keep them on.
          </p>
        ) : (
          <div className="space-y-1.5">
            {contacts.slice(0, PREVIEW_COUNT).map((contact) => (
              <IdentityContactRow
                key={contact.owner_id}
                contact={contact}
                isPending={pendingContactIds.has(contact.owner_id)}
                onToggle={(next) => toggle(contact.owner_id, next)}
              />
            ))}
            {totalCount > PREVIEW_COUNT && (
              <Button
                variant="link"
                size="sm"
                className="px-0"
                onClick={() => setIsAllOpen(true)}
              >
                Show all ({totalCount})
              </Button>
            )}
            <IdentityConsentFootnote />
          </div>
        )}
      </CardContent>

      <AllIdentityContactsSheet
        contacts={contacts}
        pendingContactIds={pendingContactIds}
        open={isAllOpen}
        onOpenChange={setIsAllOpen}
        onToggle={toggle}
      />
    </Card>
  )
}
