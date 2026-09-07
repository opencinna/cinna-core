import { useQuery } from "@tanstack/react-query"
import { createFileRoute, redirect, useNavigate } from "@tanstack/react-router"
import { AlertTriangle, Filter } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import {
  type ManagedAICredentialPublic,
  AdminLlmProvidersService,
  AdminProviderCredentialsService,
} from "@/client"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { LlmProvidersTable } from "@/components/Admin/LlmProviders/LlmProvidersTable"
import { hasKeyInFlight } from "@/components/Admin/LlmProviders/MemberKeyStatus"
import { ManagedCredentialDialog } from "@/components/Admin/LlmProviders/ManagedCredentialDialog"
import {
  managedCredentialsQueryKey,
  PROVIDER_ADMIN_CREDENTIALS_QUERY_KEY,
} from "@/components/Admin/LlmProviders/providerTypes"
import { ProviderAdminCredentialDialog } from "@/components/Admin/ProviderAdminCredentials/ProviderAdminCredentialDialog"
import { ProviderAdminCredentialsTable } from "@/components/Admin/ProviderAdminCredentials/ProviderAdminCredentialsTable"
import PendingItems from "@/components/Pending/PendingItems"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

/**
 * The invitation flow's hand-off.
 *
 * `?newCredentialFor=<user id>&label=<display name>` means "an admin has just
 * invited this person and wants to give them a key". It exists so the success
 * screen of the invite wizard can *link* onward instead of opening a second
 * dialog on top of itself: the link has to land somewhere that does the thing,
 * not on a page where the admin has to find the person again.
 */
type AiCredentialsSearch = {
  newCredentialFor?: string
  label?: string
}

export const Route = createFileRoute("/_layout/admin/ai-credentials")({
  component: AdminAiCredentials,
  validateSearch: (search: Record<string, unknown>): AiCredentialsSearch => ({
    newCredentialFor:
      typeof search.newCredentialFor === "string" && search.newCredentialFor
        ? search.newCredentialFor
        : undefined,
    label:
      typeof search.label === "string" && search.label
        ? search.label
        : undefined,
  }),
  head: () => ({
    meta: [
      {
        title: `AI Credentials - Admin - ${APP_NAME}`,
      },
    ],
  }),
  beforeLoad: async ({ context }) => {
    if (!isLoggedIn()) {
      throw redirect({ to: "/login" })
    }
    // context.user is populated by the _layout auth guard.
    const user = (context as any)?.user
    if (user && !user.is_superuser) {
      throw redirect({ to: "/" })
    }
  },
})

function AdminAiCredentials() {
  const { setHeaderContent } = usePageHeader()
  const { user } = useAuth()
  const { showSuccessToast } = useCustomToast()
  const search = Route.useSearch()
  const navigate = useNavigate()

  // The hand-off, latched out of the URL and then stripped from it, the same
  // way `credential/$credentialId` latches `?new=1`: a refresh or a Back must
  // not reopen a create dialog the admin has already dealt with, and the
  // marker must not survive into a link the admin copies.
  //
  // Latched per target rather than per mount, because the router reuses this
  // component across a search change — arriving here a second time, for a
  // different person, has to re-arm.
  const [handoffTarget, setHandoffTarget] = useState<string | null>(
    () => search.newCredentialFor ?? null,
  )
  const [handoff, setHandoff] = useState<{ id: string; label: string } | null>(
    () =>
      search.newCredentialFor
        ? { id: search.newCredentialFor, label: search.label ?? "" }
        : null,
  )
  // Tracks the marker's *absence* as well as its presence. The idiom this
  // copies latches on `credentialId`, a path identity that outlives the
  // `?new=1` marker; here the identity is the marker and the effect below
  // deletes it, so a latch that only moved on a new id would keep the last one
  // forever — and a second link for the *same* person would silently do
  // nothing.
  const marker = search.newCredentialFor ?? null
  if (marker !== handoffTarget) {
    setHandoffTarget(marker)
    if (marker) setHandoff({ id: marker, label: search.label ?? "" })
  }

  useEffect(() => {
    if (search.newCredentialFor !== undefined || search.label !== undefined) {
      navigate({ to: "/admin/ai-credentials", search: {}, replace: true })
    }
  }, [search.newCredentialFor, search.label, navigate])

  // What is still *owed* after a key was added — never a receipt.
  //
  // The three outcomes are not the same kind of fact. "Key added" and "the key
  // is being created" are results, and results are toasts. `needs_key` is a
  // live prerequisite: the credential exists but is not that person's default,
  // so nothing will pick it up, and the admin has one more thing to do. That
  // one stays on the page, as an alert carrying the action that discharges it.
  //
  // Read from the *server's* answer for this person (`api_key_onboarding_state`
  // — the same field their own paste-a-key wall reads), never inferred from
  // having created a row: `set_as_default` defaults to false, so a perfectly
  // good credential can leave its only member walled.
  const [keyNotice, setKeyNotice] = useState<{
    record: ManagedAICredentialPublic
    userId: string
    label: string
  } | null>(null)
  const [noticeEditOpen, setNoticeEditOpen] = useState(false)

  // Single-user filter, modeled as a 0-or-1 entry selection so it can reuse
  // the shared UserAllowlistPicker. The filter panel is hidden by default and
  // toggled open via the "Filter" button in the page header. Maps to the
  // backend `target_user_id` query param (records that have that user as a
  // member).
  const [filterUser, setFilterUser] = useState<UserAllowlistSelectedItem | null>(null)
  const [showFilter, setShowFilter] = useState(false)
  const targetUserId = filterUser?.userId ?? undefined

  // Two surfaces that configure each other: a managed credential can only mint
  // through a provider organisation connected on the second tab, so they live
  // on one page rather than on two pages an admin has to know to visit in
  // order.
  const [tab, setTab] = useState<"managed" | "provider-keys">("managed")

  // Client-side pagination over the full managed-credential list.
  const PAGE_SIZE = 10
  const [page, setPage] = useState(1)

  // Reset to the first page whenever the active filter changes.
  useEffect(() => {
    setPage(1)
  }, [targetUserId])

  const {
    data: records,
    isLoading,
    isError,
  } = useQuery({
    queryKey: managedCredentialsQueryKey(targetUserId),
    queryFn: () =>
      AdminLlmProvidersService.listManagedAiCredentials({ targetUserId }),
    staleTime: 30_000,
    // A key being created is the one thing on this page that moves without an
    // admin doing anything, so the list follows it and stops when it settles.
    // The predicate reads the status the server stated on each member; it does
    // not decide for itself which members are still working.
    refetchInterval: (query) =>
      tab === "managed" && (query.state.data ?? []).some(hasKeyInFlight)
        ? 10_000
        : false,
  })

  const {
    data: providerKeys,
    isError: providerKeysError,
  } = useQuery({
    queryKey: PROVIDER_ADMIN_CREDENTIALS_QUERY_KEY,
    queryFn: () =>
      AdminProviderCredentialsService.listProviderAdminCredentials(),
    staleTime: 30_000,
    enabled: tab === "provider-keys",
  })

  // The alert answers to the server, not to us: once this member's state stops
  // being `needs_key` — the admin set the default here, or the person chose it
  // themselves in Settings — the reason for the alert is gone and so is the
  // alert. A record missing from the current view (the filter above can hide
  // it) is not evidence of anything, so the notice stands until dismissed.
  const noticeRecord = keyNotice
    ? ((records ?? []).find((row) => row.id === keyNotice.record.id) ??
      keyNotice.record)
    : null
  const noticeMember =
    keyNotice && noticeRecord
      ? noticeRecord.members?.find((m) => m.user_id === keyNotice.userId)
      : undefined
  // No member row left means the grant itself is gone — removed here or the
  // record deleted — and nothing is owed on it any more. Defaulting that case
  // to `needs_key` would pin an alert open that only Dismiss could clear.
  const noticeStillOwed = noticeMember?.api_key_onboarding_state === "needs_key"

  // Sort by name for stable ordering across refetches.
  const sortedRecords = useMemo(
    () => [...(records ?? [])].sort((a, b) => a.name.localeCompare(b.name)),
    [records],
  )

  // Derived pagination over records: clamp the active page and slice the
  // current page out of the list.
  const totalPages = Math.max(1, Math.ceil(sortedRecords.length / PAGE_SIZE))
  const currentPage = Math.min(page, totalPages)
  const pagedRecords = useMemo(
    () => sortedRecords.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE),
    [sortedRecords, currentPage],
  )

  useEffect(() => {
    setHeaderContent(
      <>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">AI Credentials</h1>
          <p className="text-xs text-muted-foreground">
            Provision AI credentials on behalf of users
          </p>
        </div>
        <div className="flex items-center gap-2">
          {tab === "managed" ? (
            <>
              <Button
                variant={showFilter || filterUser ? "secondary" : "outline"}
                size="sm"
                onClick={() => setShowFilter((v) => !v)}
              >
                <Filter className="mr-2 h-4 w-4" />
                Filter
                {filterUser && (
                  <span className="ml-2 inline-block size-2 rounded-full bg-primary" />
                )}
              </Button>
              <ManagedCredentialDialog mode="create" />
            </>
          ) : (
            <ProviderAdminCredentialDialog mode="create" />
          )}
        </div>
      </>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, showFilter, filterUser, tab])

  // Edge-case guard for a non-superuser that slipped past beforeLoad.
  if (user && !user.is_superuser) {
    return (
      <div className="p-6 text-center text-muted-foreground">
        You do not have permission to view this page.
      </div>
    )
  }

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl space-y-4">
        {keyNotice && noticeStillOwed && (
          <Alert>
            <AlertTriangle />
            <AlertTitle>Key added — not their default</AlertTitle>
            <AlertDescription>
              <p>
                {keyNotice.label || "That account"} has a key on "
                {noticeRecord?.name ?? keyNotice.record.name}", but it is not
                set as their default, so nothing will pick it up yet. Set it as
                their default here — or they can choose it themselves in
                Settings.
              </p>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setNoticeEditOpen(true)}
                >
                  Open credential
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setKeyNotice(null)
                    // Both, always: a stuck `true` would pop the edit dialog
                    // unbidden the next time a notice appears.
                    setNoticeEditOpen(false)
                  }}
                >
                  Dismiss
                </Button>
              </div>
            </AlertDescription>
          </Alert>
        )}

        {keyNotice && noticeRecord && (
          <ManagedCredentialDialog
            mode="edit"
            record={noticeRecord}
            open={noticeEditOpen}
            onOpenChange={setNoticeEditOpen}
          />
        )}

        {/* Controlled, and mounted only while the hand-off is live: the page
            header already carries this dialog's own uncontrolled instance for
            an admin who came here to create a credential from scratch. */}
        {handoff && (
          <ManagedCredentialDialog
            mode="create"
            open
            onOpenChange={(open) => {
              if (!open) setHandoff(null)
            }}
            initialTargets={[
              {
                id: handoff.id,
                userId: handoff.id,
                // Keeps the preselected member from rendering as "Unknown
                // user" until the picker's own lookup resolves.
                fallbackLabel: handoff.label,
              },
            ]}
            nameSubject={handoff.label}
            onCreated={(created) => {
              const state =
                created.record.members?.find((m) => m.user_id === handoff.id)
                  ?.api_key_onboarding_state ?? "needs_key"
              if (state === "has_key") {
                showSuccessToast("Key added.")
                return
              }
              if (state === "preparing") {
                showSuccessToast("The key is being created now.")
                return
              }
              setKeyNotice({
                record: created.record,
                userId: handoff.id,
                label: handoff.label,
              })
            }}
          />
        )}

        <Tabs
          value={tab}
          onValueChange={(value) => setTab(value as "managed" | "provider-keys")}
          className="space-y-4"
        >
          <TabsList>
            <TabsTrigger value="managed">Managed credentials</TabsTrigger>
            <TabsTrigger value="provider-keys">Provider keys</TabsTrigger>
          </TabsList>

          <TabsContent value="managed" className="space-y-4">
            {/* Filter by target user — toggled via the header "Filter" button */}
            {showFilter && (
              <div className="flex flex-col gap-2 sm:max-w-md rounded-md border bg-muted/30 p-3">
                <UserAllowlistPicker
                  label="Filter by target user"
                  searchPlaceholder="Search a user to filter..."
                  selected={filterUser ? [filterUser] : []}
                  onAdd={(u) =>
                    setFilterUser({
                      id: u.id,
                      userId: u.id,
                      fallbackLabel: u.full_name || u.email,
                    })
                  }
                  onRemove={() => setFilterUser(null)}
                />
                {filterUser && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="self-start h-7 px-2 text-xs"
                    onClick={() => setFilterUser(null)}
                  >
                    Clear filter
                  </Button>
                )}
              </div>
            )}

            {isLoading ? (
              <PendingItems />
            ) : isError || !records ? (
              <div className="flex flex-col items-center justify-center gap-2 py-20 text-center">
                <p className="text-muted-foreground">
                  Failed to load credentials. Please try refreshing the page.
                </p>
              </div>
            ) : (
              <>
                <LlmProvidersTable records={pagedRecords} />
                {totalPages > 1 && (
                  <Pagination>
                    <PaginationContent>
                      <PaginationItem>
                        <PaginationPrevious
                          href="#"
                          aria-disabled={currentPage <= 1}
                          className={
                            currentPage <= 1 ? "pointer-events-none opacity-50" : undefined
                          }
                          onClick={(e) => {
                            e.preventDefault()
                            setPage((p) => Math.max(1, p - 1))
                          }}
                        />
                      </PaginationItem>
                      {Array.from({ length: totalPages }, (_, i) => i + 1).map((p) => (
                        <PaginationItem key={p}>
                          <PaginationLink
                            href="#"
                            isActive={p === currentPage}
                            onClick={(e) => {
                              e.preventDefault()
                              setPage(p)
                            }}
                          >
                            {p}
                          </PaginationLink>
                        </PaginationItem>
                      ))}
                      <PaginationItem>
                        <PaginationNext
                          href="#"
                          aria-disabled={currentPage >= totalPages}
                          className={
                            currentPage >= totalPages
                              ? "pointer-events-none opacity-50"
                              : undefined
                          }
                          onClick={(e) => {
                            e.preventDefault()
                            setPage((p) => Math.min(totalPages, p + 1))
                          }}
                        />
                      </PaginationItem>
                    </PaginationContent>
                  </Pagination>
                )}
              </>
            )}
          </TabsContent>

          <TabsContent value="provider-keys" className="space-y-4">
            <p className="max-w-3xl text-sm text-muted-foreground">
              Provider organisations this instance can create API keys in. A
              managed credential set to give each member their own key mints it
              through one of these. Each key is created inside the configured
              project, and the project's monthly spend limit is verified as
              enforcing before the first key is created.
            </p>
            {/* Error first, then "no data yet". The query is disabled while the
                other tab is showing, and a disabled query is not loading — an
                `isLoading ? … : !data ? error` ladder would flash the failure
                message on the first render after the tab switch. */}
            {providerKeysError ? (
              <div className="flex flex-col items-center justify-center gap-2 py-20 text-center">
                <p className="text-muted-foreground">
                  Failed to load provider keys. Please try refreshing the page.
                </p>
              </div>
            ) : !providerKeys ? (
              <PendingItems />
            ) : (
              <ProviderAdminCredentialsTable records={providerKeys} />
            )}
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}
