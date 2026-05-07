import { useQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Key } from "lucide-react"
import { useEffect } from "react"

import { CredentialsService } from "@/client"
import AddCredential from "@/components/Credentials/AddCredential"
import { CredentialCard } from "@/components/Credentials/CredentialCard"
import { SharedWithMeCredentials } from "@/components/Credentials/SharedWithMeCredentials"
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

function CredentialsGrid() {
  const { workspaceFilter } = useWorkspace()

  const { data, isLoading, error } = useQuery({
    queryKey: ["credentials", workspaceFilter],
    queryFn: async ({ queryKey }) => {
      const [, workspaceId] = queryKey
      const response = await CredentialsService.readCredentials({
        skip: 0,
        limit: 100,
        userWorkspaceId: workspaceId as string | undefined,
      })
      return response
    },
  })

  if (isLoading) {
    return <PendingItems />
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">
          Error loading credentials: {(error as Error).message}
        </p>
      </div>
    )
  }

  const credentials = data?.data || []

  if (credentials.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center text-center py-12">
        <div className="rounded-full bg-muted p-4 mb-4">
          <Key className="h-8 w-8 text-muted-foreground" />
        </div>
        <h3 className="text-lg font-semibold">
          You don't have any credentials yet
        </h3>
        <p className="text-muted-foreground">Add a new credential to get started</p>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 auto-rows-fr">
      {credentials.map((credential) => (
        <CredentialCard key={credential.id} credential={credential} />
      ))}
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
        {/* My Credentials */}
        <div className="space-y-4">
          <h2 className="text-lg font-semibold">My Credentials</h2>
          <CredentialsGrid key={activeWorkspaceId ?? 'default'} />
        </div>

        {/* Shared With Me */}
        <SharedWithMeCredentials />
      </div>
    </div>
  )
}
