/**
 * "Shared through your bundles" — on a connection shared *with* the viewer:
 * the people who got it from its owner by installing a bundle or skill the
 * viewer published, and a way to cut one of them off (S4b).
 *
 * P5 in the connected-machines shape (`UserSettings/DesktopSessionsCard`):
 * five rows, "Show all (N)" to a Sheet, one hover-revealed destructive icon
 * per row. The list is a by-product of publishing with no Create action here,
 * so the card is not rendered once it is known to be empty — but it is while
 * loading or failed, so a failure never reads as "nobody".
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Forward } from "lucide-react"
import { useMemo, useState } from "react"

import { type CredentialPublic, CredentialsService } from "@/client"
import { PreviewList } from "@/components/Common/PreviewList"
import { parseTimestamp } from "@/components/Common/RelativeTime"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { CredentialReshareRow } from "./CredentialReshareRow"
import { CredentialUsagesSheet } from "./CredentialUsagesSheet"

export function credentialResharesQueryKey(credentialId: string) {
  return ["credential-reshares", credentialId] as const
}

export function CredentialResharesCard({
  credential,
}: {
  credential: CredentialPublic
}) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [sheetOpen, setSheetOpen] = useState(false)
  const queryKey = credentialResharesQueryKey(credential.id)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey,
    queryFn: () =>
      CredentialsService.getCredentialReshares({ credentialId: credential.id }),
  })

  const revokeMutation = useMutation({
    mutationFn: (shareId: string) =>
      CredentialsService.revokeCredentialShare({
        credentialId: credential.id,
        shareId,
      }),
    onSuccess: () => showSuccessToast("Access removed"),
    onError: handleError.bind(showErrorToast),
    // On both paths: a share already gone 404s, and the refetch clears it.
    onSettled: () => queryClient.invalidateQueries({ queryKey }),
  })

  // Most recent first (P5): the endpoint returns them oldest first. One
  // order for the card and the Sheet.
  const shares = useMemo(
    () =>
      [...(data?.data ?? [])].sort(
        (a, b) =>
          (parseTimestamp(b.shared_at)?.getTime() ?? 0) -
          (parseTimestamp(a.shared_at)?.getTime() ?? 0),
      ),
    [data],
  )
  // "Down" is an error with nothing cached.
  const isDown = isError && !data
  if (!isLoading && !isDown && shares.length === 0) return null

  const owner = credential.owner_email || ""
  const renderRow = (share: (typeof shares)[number]) => (
    <CredentialReshareRow
      key={share.id}
      share={share}
      credentialName={credential.name}
      ownerEmail={owner}
      isPending={
        revokeMutation.isPending && revokeMutation.variables === share.id
      }
      onRemove={(shareId, onDone) =>
        revokeMutation.mutate(shareId, { onSuccess: onDone })
      }
    />
  )

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <Forward className="h-5 w-5 shrink-0" />
          Shared through your bundles
        </CardTitle>
        <CardDescription>
          People who got this connection from {owner || "its owner"} by
          installing a bundle or skill you published.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <PreviewList
          items={shares}
          getKey={(share) => share.id}
          renderItem={renderRow}
          isLoading={isLoading}
          isError={isDown}
          error={error}
          onRetry={() => refetch()}
          errorFallback="Couldn't load the people you shared it with"
          empty={null}
          skeletonRows={2}
          skeletonClassName="h-[48px] w-full rounded-md"
          onShowAll={() => setSheetOpen(true)}
        />
      </CardContent>

      <CredentialUsagesSheet
        title="Shared through your bundles"
        description={`${shares.length === 1 ? "1 person" : `${shares.length} people`} who got it through a bundle or skill you published`}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
      >
        {shares.map(renderRow)}
      </CredentialUsagesSheet>
    </Card>
  )
}
