import { useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"
import { AlertTriangle, Search, X } from "lucide-react"
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react"

import { type CredentialPublic, CredentialsService } from "@/client"
import { DataTable } from "@/components/Common/DataTable"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import AddCredential from "@/components/Credentials/AddCredential"
import {
  type CredentialSection,
  CredentialSectionTabs,
} from "@/components/Credentials/CredentialSectionTabs"
import {
  type CredentialRowModel,
  getCredentialColumns,
} from "@/components/Credentials/credentialColumns"
import { getCredentialTypeMeta } from "@/components/Credentials/credentialTypes"
import DeleteCredential, {
  deleteCredentialMutationKey,
} from "@/components/Credentials/DeleteCredential"
import PendingCredentials from "@/components/Pending/PendingCredentials"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
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

// Hash deep-linking for the active section — the HashTabs idiom (hash
// slice/assign + a hashchange listener). The token differs from the section
// value only for "My Credentials" (#my ↔ "mine").
const SECTION_TO_HASH: Record<CredentialSection, string> = {
  mine: "my",
  automatic: "automatic",
  bundle: "bundle",
}
const HASH_TO_SECTION: Record<string, CredentialSection> = {
  my: "mine",
  automatic: "automatic",
  bundle: "bundle",
}

function getInitialSection(): CredentialSection {
  return HASH_TO_SECTION[window.location.hash.slice(1)] ?? "mine"
}

const PAGE_SIZE = 100

/**
 * Every credential the caller owns, page by page. The endpoint caps a page,
 * and a single fixed-size read silently dropped every credential past it.
 * Rows are keyed by id, so a row that shifts between two page reads is not
 * listed twice.
 */
async function readAllOwnedCredentials(
  workspaceId: string | undefined,
): Promise<CredentialPublic[]> {
  const byId = new Map<string, CredentialPublic>()
  let skip = 0
  for (;;) {
    const page = await CredentialsService.readCredentials({
      skip,
      limit: PAGE_SIZE,
      userWorkspaceId: workspaceId,
    })
    for (const credential of page.data) byId.set(credential.id, credential)
    skip += page.data.length
    if (page.data.length === 0 || skip >= page.count) break
  }
  return [...byId.values()]
}

function matchesQuery(credential: CredentialRowModel, query: string): boolean {
  const type = getCredentialTypeMeta(credential.type).label
  return [credential.name, credential.notes, type, credential.owner_email]
    .filter(Boolean)
    .some((field) => (field as string).toLowerCase().includes(query))
}

const EMPTY_SECTION: Record<CredentialSection, () => ReactNode> = {
  mine: () => (
    <EmptySection text="No credentials yet.">
      <AddCredential trigger={<Button size="sm">Add credential</Button>} />
    </EmptySection>
  ),
  automatic: () => (
    <EmptySection text="Connections you make with Connect Agent API or Connect MCP Provider, and credentials shared with you by skills you add, appear here.">
      <AddCredential trigger={<Button size="sm">Add credential</Button>} />
    </EmptySection>
  ),
  bundle: () => (
    <EmptySection text="Credentials shared with you by installed bundles appear here.">
      <Button size="sm" variant="outline" asChild>
        <Link to="/catalog">Browse the catalog</Link>
      </Button>
    </EmptySection>
  ),
}

function EmptySection({
  text,
  children,
}: {
  text: string
  children?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center gap-3 py-6 whitespace-normal">
      <p className="max-w-md text-sm text-muted-foreground">{text}</p>
      {children}
    </div>
  )
}

function CredentialsList() {
  const { workspaceFilter } = useWorkspace()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [section, setSection] = useState<CredentialSection>(getInitialSection)
  const [search, setSearch] = useState("")
  const searchRef = useRef<HTMLInputElement>(null)
  // Both clear controls unmount on click (the X and the no-match link), so
  // focus moves to the input rather than falling to the page.
  const clearSearch = () => {
    setSearch("")
    searchRef.current?.focus()
  }
  const [deleteTarget, setDeleteTarget] = useState<CredentialRowModel | null>(
    null,
  )

  const handleSectionChange = (next: CredentialSection) => {
    setSection(next)
    window.location.hash = SECTION_TO_HASH[next]
  }

  useEffect(() => {
    const handleHashChange = () => {
      const next = HASH_TO_SECTION[window.location.hash.slice(1)]
      if (next) setSection(next)
    }
    window.addEventListener("hashchange", handleHashChange)
    return () => window.removeEventListener("hashchange", handleHashChange)
  }, [])

  const owned = useQuery({
    // Its own key under the `["credentials"]` prefix (so every credential
    // mutation still invalidates it): the shorter key belongs to the
    // first-page reads other surfaces make, and holds a different shape.
    queryKey: ["credentials", workspaceFilter, "all-pages"],
    queryFn: () => readAllOwnedCredentials(workspaceFilter),
  })

  // Unpaged on the server: the endpoint returns every share in one response.
  const shared = useQuery({
    queryKey: ["credentials-shared-with-me"],
    queryFn: () => CredentialsService.getCredentialsSharedWithMe(),
  })

  // Owned + shared as one list of row models, by name. The server-computed
  // `category` decides each row's section; the page never re-derives it.
  const rows: CredentialRowModel[] = useMemo(() => {
    const ownedRows: CredentialRowModel[] = (owned.data ?? []).map((c) => ({
      id: c.id,
      name: c.name,
      type: c.type,
      notes: c.notes,
      category: (c.category || "mine") as CredentialSection,
      agent_usage_count: c.agent_usage_count ?? 0,
      used_in_bundle: c.used_in_bundle ?? false,
      is_shared: false,
      share_count: c.share_count ?? 0,
      group_share_count: c.group_share_count ?? 0,
      status: c.status,
      agent_api_key_status: c.agent_api_key_status,
    }))
    const sharedRows: CredentialRowModel[] = (shared.data?.data ?? []).map(
      (c) => ({
        id: c.id,
        name: c.name,
        type: c.type,
        notes: c.notes,
        category: (c.category || "mine") as CredentialSection,
        agent_usage_count: c.agent_usage_count ?? 0,
        used_in_bundle: c.used_in_bundle ?? false,
        is_shared: true,
        owner_email: c.owner_email,
        shared_at: c.shared_at,
        via_groups: (c.via_groups ?? []).map((g) => g.name),
      }),
    )
    return [...ownedRows, ...sharedRows].sort((a, b) =>
      a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
    )
  }, [owned.data, shared.data])

  const counts = useMemo(() => {
    const result: Record<CredentialSection, number> = {
      mine: 0,
      automatic: 0,
      bundle: 0,
    }
    for (const row of rows) {
      if (row.category in result) result[row.category] += 1
    }
    return result
  }, [rows])

  const query = search.trim().toLowerCase()
  const visibleRows = useMemo(
    () =>
      rows.filter(
        (row) =>
          row.category === section && (!query || matchesQuery(row, query)),
      ),
    [rows, section, query],
  )

  const columns = useMemo(
    () => getCredentialColumns({ onDelete: setDeleteTarget }),
    [],
  )

  if (owned.isError && owned.data === undefined) {
    return (
      <QueryErrorAlert
        error={owned.error}
        fallback="Couldn't load your credentials."
        onRetry={() => owned.refetch()}
      />
    )
  }

  // Rows never pop in: the table waits for both lists (or the shared one to
  // fail, which the alert below reports).
  const loading =
    owned.data === undefined || (shared.data === undefined && !shared.isError)

  const emptyState = query ? (
    <EmptySection text={`No credentials match "${search.trim()}".`}>
      <Button variant="link" size="sm" onClick={clearSearch}>
        Clear search
      </Button>
    </EmptySection>
  ) : shared.isError ? (
    <EmptySection text="Nothing to show here until the credentials shared with you load." />
  ) : (
    EMPTY_SECTION[section]()
  )

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <CredentialSectionTabs
          value={section}
          onChange={handleSectionChange}
          counts={loading ? undefined : counts}
        />
        <div className="relative min-w-40 max-w-64 flex-1">
          <Search className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            ref={searchRef}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search"
            aria-label="Search credentials by name, notes, type or owner"
            className="pl-9 pr-9"
          />
          {search && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="absolute top-1/2 right-1 h-7 w-7 -translate-y-1/2"
                  onClick={clearSearch}
                  aria-label="Clear search"
                >
                  <X className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                Clear search
              </TooltipContent>
            </Tooltip>
          )}
        </div>
      </div>

      {shared.isError && (
        <Alert className="border-warning/50 text-warning">
          <AlertTriangle />
          <AlertTitle>
            Credentials shared with you could not be loaded.
          </AlertTitle>
          <AlertDescription className="flex flex-wrap items-center gap-2">
            <span>All three sections may be missing rows.</span>
            <Button
              variant="outline"
              size="sm"
              className="h-7"
              onClick={() => shared.refetch()}
            >
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {/* An empty section says the same thing in its empty state. */}
      {section === "automatic" && !loading && counts.automatic > 0 && (
        <p className="text-sm text-muted-foreground">
          These are made by Connect Agent API and Connect MCP Provider, or
          shared with you by skills you add.
        </p>
      )}

      {loading ? (
        <PendingCredentials />
      ) : (
        <DataTable
          // A new section or search starts at page 1 (remount); a refetch
          // keeps the page the user is on.
          key={`${section}|${query}`}
          autoResetPageIndex={false}
          columns={columns}
          data={visibleRows}
          getRowId={(row) => row.id}
          tableClassName="table-fixed"
          // A row reads on its own — name, badges, menu — so the column
          // names would only label the obvious.
          hideHeader
          itemLabel="credentials"
          emptyState={emptyState}
          onRowClick={(row) => {
            // A row whose delete is in flight is not a way into it.
            if (
              queryClient.isMutating({
                mutationKey: deleteCredentialMutationKey(row.id),
              }) > 0
            ) {
              return
            }
            navigate({
              to: "/credential/$credentialId",
              params: { credentialId: row.id },
            })
          }}
        />
      )}

      {deleteTarget && (
        <DeleteCredential
          credential={deleteTarget}
          isOpen
          setIsOpen={(open) => {
            if (!open) setDeleteTarget(null)
          }}
          onSuccess={() => setDeleteTarget(null)}
        />
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
          <p className="text-xs text-muted-foreground">
            Securely store and manage credentials
          </p>
        </div>
        <AddCredential />
      </>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        {/* Owned + shared credentials share one sectioned table; remount on
            workspace change to reset the section's query state. */}
        <CredentialsList key={activeWorkspaceId ?? "default"} />
      </div>
    </div>
  )
}
