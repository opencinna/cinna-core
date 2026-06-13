import { useQuery } from "@tanstack/react-query"
import { createFileRoute, redirect } from "@tanstack/react-router"
import { Filter } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import { AdminLlmProvidersService, UsersService } from "@/client"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { LlmProvidersTable } from "@/components/Admin/LlmProviders/LlmProvidersTable"
import { ProvisionLlmProviderDialog } from "@/components/Admin/LlmProviders/ProvisionLlmProviderDialog"
import { managedCredentialsQueryKey } from "@/components/Admin/LlmProviders/providerTypes"
import PendingItems from "@/components/Pending/PendingItems"
import { Button } from "@/components/ui/button"
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/admin/llm-providers")({
  component: AdminLlmProviders,
  head: () => ({
    meta: [
      {
        title: `LLM Providers - Admin - ${APP_NAME}`,
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

function AdminLlmProviders() {
  const { setHeaderContent } = usePageHeader()
  const { user } = useAuth()

  // Single-user filter, modeled as a 0-or-1 entry selection so it can reuse
  // the shared UserAllowlistPicker. The filter panel is hidden by default and
  // toggled open via the "Filter" button in the page header.
  const [filterUser, setFilterUser] = useState<UserAllowlistSelectedItem | null>(null)
  const [showFilter, setShowFilter] = useState(false)
  const targetUserId = filterUser?.userId ?? undefined

  // Client-side pagination over the full managed-credential list.
  const PAGE_SIZE = 10
  const [page, setPage] = useState(1)

  // Reset to the first page whenever the active filter changes.
  useEffect(() => {
    setPage(1)
  }, [targetUserId])

  const {
    data: credentials,
    isLoading,
    isError,
  } = useQuery({
    queryKey: managedCredentialsQueryKey(targetUserId),
    queryFn: () =>
      AdminLlmProvidersService.listManagedAiCredentials({ targetUserId }),
    staleTime: 30_000,
  })

  // Resolve owner display labels from the admin user list. Superusers have
  // access to GET /users/, so this is a single fetch reused for all rows.
  const { data: users } = useQuery({
    queryKey: ["users", "for-llm-providers"],
    queryFn: () => UsersService.readUsers({ skip: 0, limit: 100 }),
  })

  const ownerLabels = useMemo(() => {
    const map: Record<string, string> = {}
    for (const u of users?.data ?? []) {
      map[u.id] = u.full_name || u.email
    }
    // Ensure the actively-filtered user is always labeled even if outside the
    // first page of the admin user list.
    if (filterUser?.userId && filterUser.fallbackLabel) {
      map[filterUser.userId] = filterUser.fallbackLabel
    }
    return map
  }, [users, filterUser])

  // Derived pagination: clamp the active page to the available range and slice
  // the current page out of the full list.
  const totalCount = credentials?.length ?? 0
  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE))
  const currentPage = Math.min(page, totalPages)
  const pagedCredentials = useMemo(
    () => (credentials ?? []).slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE),
    [credentials, currentPage],
  )

  useEffect(() => {
    setHeaderContent(
      <>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">LLM Providers</h1>
          <p className="text-xs text-muted-foreground">
            Provision read-only AI credentials on behalf of users
          </p>
        </div>
        <div className="flex items-center gap-2">
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
          <ProvisionLlmProviderDialog />
        </div>
      </>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, showFilter, filterUser])

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
        ) : isError || !credentials ? (
          <div className="flex flex-col items-center justify-center gap-2 py-20 text-center">
            <p className="text-muted-foreground">
              Failed to load credentials. Please try refreshing the page.
            </p>
          </div>
        ) : (
          <>
            <LlmProvidersTable credentials={pagedCredentials} ownerLabels={ownerLabels} />
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
      </div>
    </div>
  )
}
