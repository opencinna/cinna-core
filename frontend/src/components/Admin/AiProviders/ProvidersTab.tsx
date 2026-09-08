import { useQuery } from "@tanstack/react-query"
import { useMemo } from "react"

import { AdminAiProvidersService } from "@/client"
import { ProviderRow } from "@/components/Admin/AiProviders/ProviderRow"
import { hasProviderKeyInFlight } from "@/components/Admin/LlmProviders/MemberKeyStatus"
import { AI_PROVIDERS_QUERY_KEY } from "@/components/Admin/LlmProviders/providerTypes"
import { ListRowGroup } from "@/components/Common/ListRow"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { useProviderAdapters } from "@/components/Admin/LlmProviders/useProviderAdapters"
import { Skeleton } from "@/components/ui/skeleton"

/**
 * Where AI keys come from, and who automatically gets one.
 *
 * **A tab-width row list, not a card and not a `DataTable`.** §2 "Card width":
 * *a table that genuinely needs its columns also needs search or pagination,
 * which makes it a Manage-list route*. There are tens of providers at most —
 * the plan's own index decision reads every row — so this list needs neither,
 * which rules the table out; and R4's cap on a list in a card would need a
 * "Show all" with nowhere to send it, which rules the card out. What is left is
 * the sibling tab's `rounded-md border` frame around a `ListRowGroup`, so the
 * two tabs frame their content identically. It has no header of its own: the
 * `TabsTrigger` label is its title, which is why R15 does not apply to it.
 *
 * The create button lives in the **page header**, beside the other tab's, so
 * this route's primary action sits where every other admin route puts it.
 */
export function ProvidersTab() {
  const { typeLabel } = useProviderAdapters()

  const {
    data: providers,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: AI_PROVIDERS_QUERY_KEY,
    queryFn: () => AdminAiProvidersService.listAiProviders(),
    staleTime: 30_000,
    // A key being created is the one thing on this tab that moves without an
    // admin doing anything, so the list follows it and stops when it settles —
    // the same rule the managed-credentials list applies, reading the same
    // `inFlight` fact off the statuses the server stated. It matters most
    // straight after "Apply to existing users" on a minted provider, which is
    // the press that puts a batch of keys into flight.
    refetchInterval: (query) =>
      (query.state.data ?? []).some(hasProviderKeyInFlight) ? 10_000 : false,
  })

  // Alphabetical, for stable ordering across refetches.
  const rows = useMemo(
    () => [...(providers ?? [])].sort((a, b) => a.name.localeCompare(b.name)),
    [providers],
  )

  return (
    <div className="space-y-4">
      <p className="max-w-3xl text-sm text-muted-foreground">
        Where AI keys come from, and who automatically gets one. A provider
        holds the key and the rule for handing it out; the vendor it talks to is
        its <strong>type</strong>.
      </p>

      {/* Error before "nothing yet", and gated on there being nothing to show:
          a background refetch that fails must not replace a live list with an
          error panel. */}
      {isError && providers === undefined ? (
        <QueryErrorAlert
          error={error}
          fallback="Couldn't load the AI providers."
          onRetry={() => refetch()}
        />
      ) : isLoading ? (
        <div className="space-y-1.5">
          {[0, 1, 2].map((index) => (
            <Skeleton key={index} className="h-[48px] w-full rounded-md" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-md border border-dashed px-3 py-16 text-center">
          <p className="text-sm text-muted-foreground">
            No key sources yet. Connect a provider to hand every new account a
            working key on day one.
          </p>
        </div>
      ) : (
        <div className="rounded-md border p-2">
          <ListRowGroup>
            {rows.map((provider) => (
              <ProviderRow
                key={provider.id}
                provider={provider}
                typeLabel={typeLabel(provider.type)}
              />
            ))}
          </ListRowGroup>
        </div>
      )}
    </div>
  )
}
