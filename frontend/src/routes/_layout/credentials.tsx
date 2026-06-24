import { useQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Key, Link2, Package } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import { CredentialsService } from "@/client"
import AddCredential from "@/components/Credentials/AddCredential"
import {
  CredentialCard,
  type CredentialCardModel,
} from "@/components/Credentials/CredentialCard"
import {
  CredentialFilters,
  type CredentialFilter,
} from "@/components/Credentials/CredentialFilters"
import PendingItems from "@/components/Pending/PendingItems"
import useWorkspace from "@/hooks/useWorkspace"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/credentials")({
  component: Credentials,
  head: () => ({
    meta: [
      {
        title: `Credentials - ${APP_NAME}`,
      },
    ],
  }),
})

function CredentialGrid({ credentials }: { credentials: CredentialCardModel[] }) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 auto-rows-fr">
      {credentials.map((credential) => (
        <CredentialCard key={credential.id} credential={credential} />
      ))}
    </div>
  )
}

const EMPTY_STATES: Record<
  CredentialFilter,
  { icon: typeof Key; title: string; subtitle: string }
> = {
  mine: {
    icon: Key,
    title: "You don't have any credentials yet",
    subtitle: "Add a new credential to get started",
  },
  automatic: {
    icon: Link2,
    title: "No automatic connections yet",
    subtitle:
      "Connections created by \"Connect Agent API\" or \"Connect MCP Provider\" appear here",
  },
  bundle: {
    icon: Package,
    title: "No bundle credentials yet",
    subtitle:
      "You haven't installed any bundle that provides a credential",
  },
}

function EmptyState({ filter }: { filter: CredentialFilter }) {
  const { icon: Icon, title, subtitle } = EMPTY_STATES[filter]
  return (
    <div className="flex flex-col items-center justify-center text-center py-12">
      <div className="rounded-full bg-muted p-4 mb-4">
        <Icon className="h-8 w-8 text-muted-foreground" />
      </div>
      <h3 className="text-lg font-semibold">{title}</h3>
      <p className="text-muted-foreground">{subtitle}</p>
    </div>
  )
}

// Hash deep-linking for the active tab — mirrors the HashTabs idiom
// (window.location.hash slice/assign + a hashchange listener). The hash token
// differs from the filter value only for "My Credentials" (#my ↔ "mine").
const FILTER_TO_HASH: Record<CredentialFilter, string> = {
  mine: "my",
  automatic: "automatic",
  bundle: "bundle",
}
const HASH_TO_FILTER: Record<string, CredentialFilter> = {
  my: "mine",
  automatic: "automatic",
  bundle: "bundle",
}

function getInitialFilter(): CredentialFilter {
  const hash = window.location.hash.slice(1) // strip the leading "#"
  return HASH_TO_FILTER[hash] ?? "mine"
}

function CredentialTabs() {
  const { workspaceFilter } = useWorkspace()
  const [filter, setFilter] = useState<CredentialFilter>(getInitialFilter)

  // Keep the URL hash in sync with the active tab, and react to browser
  // back/forward hash changes — same pattern as HashTabs.
  const handleFilterChange = (next: CredentialFilter) => {
    setFilter(next)
    window.location.hash = FILTER_TO_HASH[next]
  }

  useEffect(() => {
    const handleHashChange = () => {
      const hash = window.location.hash.slice(1)
      const next = HASH_TO_FILTER[hash]
      if (next) {
        setFilter(next)
      }
    }
    window.addEventListener("hashchange", handleHashChange)
    return () => window.removeEventListener("hashchange", handleHashChange)
  }, [])

  const owned = useQuery({
    queryKey: ["credentials", workspaceFilter],
    queryFn: async ({ queryKey }) => {
      const [, workspaceId] = queryKey
      return CredentialsService.readCredentials({
        skip: 0,
        limit: 100,
        userWorkspaceId: workspaceId as string | undefined,
      })
    },
  })

  const shared = useQuery({
    queryKey: ["credentials-shared-with-me"],
    queryFn: () => CredentialsService.getCredentialsSharedWithMe(),
  })

  // Merge owned + shared into one categorized list of card view-models. The
  // server-computed ``category`` decides each card's tab; the frontend never
  // re-derives provenance or automatic-ness.
  const merged: CredentialCardModel[] = useMemo(() => {
    const ownedModels: CredentialCardModel[] = (owned.data?.data ?? []).map(
      (c) => ({
        id: c.id,
        name: c.name,
        type: c.type,
        notes: c.notes,
        category: c.category ?? "mine",
        agent_usage_count: c.agent_usage_count ?? 0,
        used_in_bundle: c.used_in_bundle ?? false,
        is_shared: false,
        allow_sharing: c.allow_sharing,
        share_count: c.share_count ?? 0,
        status: c.status,
      }),
    )
    const sharedModels: CredentialCardModel[] = (shared.data?.data ?? []).map(
      (c) => ({
        id: c.id,
        name: c.name,
        type: c.type,
        notes: c.notes,
        category: c.category ?? "mine",
        agent_usage_count: c.agent_usage_count ?? 0,
        used_in_bundle: c.used_in_bundle ?? false,
        is_shared: true,
        owner_email: c.owner_email,
        shared_at: c.shared_at,
      }),
    )
    return [...ownedModels, ...sharedModels]
  }, [owned.data, shared.data])

  if (owned.isLoading) {
    return <PendingItems />
  }

  if (owned.error) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">
          Error loading credentials: {(owned.error as Error).message}
        </p>
      </div>
    )
  }

  const filtered = merged.filter((c) => c.category === filter)

  return (
    <div className="space-y-4">
      <CredentialFilters value={filter} onChange={handleFilterChange} />

      {shared.error && (
        <p className="text-sm text-destructive">
          Could not load shared credentials; the Bundle and My Credentials tabs
          may be incomplete.
        </p>
      )}

      {filter === "automatic" && (
        <p className="text-sm text-muted-foreground">
          Connections created by "Connect Agent API" or "Connect MCP Provider".
          Manage name, notes, and sharing here.
        </p>
      )}

      {filtered.length > 0 ? (
        <CredentialGrid credentials={filtered} />
      ) : (
        // When the shared fetch failed, the warning banner above already
        // explains the empty grid — don't also tell the user "nothing here".
        !shared.error && <EmptyState filter={filter} />
      )}
    </div>
  )
}

function Credentials() {
  const { setHeaderContent } = usePageHeader()
  const { activeWorkspaceId } = useWorkspace()

  useEffect(() => {
    setHeaderContent(
      <>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">Credentials</h1>
          <p className="text-xs text-muted-foreground">Securely store and manage credentials</p>
        </div>
        <AddCredential />
      </>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl space-y-8">
        {/* Owned + shared credentials share one categorized view; remount on
            workspace change to reset the underlying query state. */}
        <CredentialTabs key={activeWorkspaceId ?? "default"} />
      </div>
    </div>
  )
}
