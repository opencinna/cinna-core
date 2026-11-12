import { useQuery } from "@tanstack/react-query"

import { ImprovementRequestsService } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { repeatedSessionIds } from "@/utils/improvementRequests"
import { ImprovementRequestRow } from "./ImprovementRequestRow"

/** The list endpoint's largest page (`limit` ≤ 200). */
const SHEET_PAGE_SIZE = 200

interface AllImprovementRequestsSheetProps {
  agentId: string
  /** The card's status filter, `undefined` for all. */
  status: string | undefined
  /** What the filter shows, for the header ("New requests"). */
  filterLabel: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onOpenRequest: (requestId: string) => void
}

/**
 * The "Show all (N)" destination for the Improvement Requests card.
 *
 * A Sheet rather than a route (P5): the list needs no search, sort or paging,
 * and a request's lifecycle lives in its own dialog, which the rows open. Same
 * row component and same filter as the card, read at the endpoint's largest
 * page; beyond that the CLI lists them all.
 */
export function AllImprovementRequestsSheet({
  agentId,
  status,
  filterLabel,
  open,
  onOpenChange,
  onOpenRequest,
}: AllImprovementRequestsSheetProps) {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["improvementRequests", agentId, status ?? "all", "sheet"],
    queryFn: () =>
      ImprovementRequestsService.listAgentImprovementRequests({
        agentId,
        status,
        limit: SHEET_PAGE_SIZE,
      }),
    enabled: open,
  })

  const requests = data?.data ?? []
  const count = data?.count ?? 0
  const repeated = repeatedSessionIds(requests)

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Improvement Requests</SheetTitle>
          <SheetDescription>
            {data
              ? `${count}${status ? ` ${filterLabel.toLowerCase()}` : ""} ${count === 1 ? "request" : "requests"}`
              : filterLabel}
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-2 overflow-y-auto px-4 pb-4">
          {isLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-[48px] w-full rounded-md" />
              <Skeleton className="h-[48px] w-full rounded-md" />
            </div>
          ) : isError && !data ? (
            <QueryErrorAlert
              error={error}
              fallback="Couldn't load improvement requests"
              onRetry={() => refetch()}
            />
          ) : (
            <>
              {requests.length === 0 && (
                <p className="text-sm text-muted-foreground">
                  {status
                    ? `No ${filterLabel.toLowerCase()} requests.`
                    : "No improvement requests yet."}
                </p>
              )}
              <ListRowGroup>
                {requests.map((request) => (
                  <ImprovementRequestRow
                    key={request.id}
                    request={request}
                    sharesSession={Boolean(
                      request.session_id && repeated.has(request.session_id),
                    )}
                    onOpen={onOpenRequest}
                  />
                ))}
              </ListRowGroup>
              {count > requests.length && (
                <p className="text-xs text-muted-foreground">
                  Showing {requests.length} of {count}. Use{" "}
                  <code className="rounded bg-muted px-1 py-0.5">
                    cinna improve list
                  </code>{" "}
                  to see them all.
                </p>
              )}
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
