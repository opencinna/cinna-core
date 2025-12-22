import { useQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Key } from "lucide-react"

import { CredentialsService } from "@/client"
import AddCredential from "@/components/Credentials/AddCredential"
import { CredentialCard } from "@/components/Credentials/CredentialCard"
import PendingItems from "@/components/Pending/PendingItems"

export const Route = createFileRoute("/_layout/credentials")({
  component: Credentials,
  head: () => ({
    meta: [
      {
        title: "Credentials - FastAPI Cloud",
      },
    ],
  }),
})

function CredentialsGrid() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["credentials"],
    queryFn: async () => {
      const response = await CredentialsService.readCredentials({
        skip: 0,
        limit: 100,
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
    <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
      {credentials.map((credential) => (
        <CredentialCard key={credential.id} credential={credential} />
      ))}
    </div>
  )
}

function Credentials() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Credentials</h1>
          <p className="text-muted-foreground">
            Securely store and manage your service credentials
          </p>
        </div>
        <AddCredential />
      </div>
      <CredentialsGrid />
    </div>
  )
}
