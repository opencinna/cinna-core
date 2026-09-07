import { useQuery } from "@tanstack/react-query"

import { AiCredentialsService, UsersService } from "@/client"
import { AICredentialsCard } from "./AICredentialsCard"
import { SdkPreferencesCard } from "./SdkPreferencesCard"

/**
 * The Settings → AI Credentials tab: two half-width cards in one grid.
 *
 * The two reads live here because both cards want the credential list — the
 * left one renders it, the right one names the credential each SDK mode and
 * the AI-functions setting resolve to. Each card owns its own states, its own
 * dialogs and its own mutations; this component owns nothing but the queries
 * and the grid.
 */
export function AICredentialsSettings() {
  const {
    data: credentialsList,
    isLoading: isCredentialsLoading,
    isError: isCredentialsError,
    error: credentialsError,
    refetch: refetchCredentials,
  } = useQuery({
    queryKey: ["aiCredentialsList"],
    queryFn: () => AiCredentialsService.listAiCredentials(),
  })

  const {
    data: status,
    isLoading: isStatusLoading,
    isError: isStatusError,
    error: statusError,
    refetch: refetchStatus,
  } = useQuery({
    queryKey: ["aiCredentialsStatus"],
    queryFn: () => UsersService.getAiCredentialsStatus(),
  })

  const credentials = credentialsList?.data ?? []

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 items-start">
        <AICredentialsCard
          credentials={credentials}
          totalCount={credentialsList?.count ?? credentials.length}
          isLoading={isCredentialsLoading}
          isError={isCredentialsError}
          error={credentialsError}
          onRetry={() => refetchCredentials()}
        />
        <SdkPreferencesCard
          status={status}
          isLoading={isStatusLoading}
          isError={isStatusError}
          error={statusError}
          onRetry={() => refetchStatus()}
          credentials={credentials}
        />
      </div>
    </div>
  )
}
