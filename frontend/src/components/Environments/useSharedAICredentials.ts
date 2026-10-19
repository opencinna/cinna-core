import { useQuery } from "@tanstack/react-query"

import { AiCredentialsService, type SharedAICredentialPublic } from "@/client"
import { viaGroupsLabel } from "@/utils/sharedWith"

export const SHARED_AI_CREDENTIALS_KEY = ["aiCredentialsSharedWithMe"]

/**
 * AI credentials other people share with the user, directly or through a
 * user group (D9), offered beside the user's own keys in environment config.
 */
export function useSharedAICredentials(
  enabled = true,
): SharedAICredentialPublic[] {
  const { data } = useQuery({
    queryKey: SHARED_AI_CREDENTIALS_KEY,
    queryFn: () => AiCredentialsService.listAiCredentialsSharedWithMe(),
    enabled,
  })
  return data?.data ?? []
}

/** "via Engineering" / "via Engineering +1", else "from ann@x.io". */
export function sharedCredentialOrigin(
  credential: SharedAICredentialPublic,
): string {
  return (
    viaGroupsLabel(credential.via_groups) ||
    `from ${credential.owner_email || "another user"}`
  )
}
