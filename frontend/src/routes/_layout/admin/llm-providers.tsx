import { useQuery } from "@tanstack/react-query"
import { createFileRoute, redirect } from "@tanstack/react-router"
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
  // the shared UserAllowlistPicker.
  const [filterUser, setFilterUser] = useState<UserAllowlistSelectedItem | null>(null)
  const targetUserId = filterUser?.userId ?? undefined

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

  useEffect(() => {
    setHeaderContent(
      <>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">LLM Providers</h1>
          <p className="text-xs text-muted-foreground">
            Provision read-only AI credentials on behalf of users
          </p>
        </div>
        <ProvisionLlmProviderDialog />
      </>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

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
        {/* Filter by target user */}
        <div className="flex flex-col gap-2 sm:max-w-md">
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
            <div className="text-xs text-muted-foreground">
              <strong>{credentials.length}</strong> managed credential
              {credentials.length !== 1 ? "s" : ""}
              {targetUserId ? " for the selected user" : " fleet-wide"}
            </div>
            <LlmProvidersTable credentials={credentials} ownerLabels={ownerLabels} />
          </>
        )}
      </div>
    </div>
  )
}
