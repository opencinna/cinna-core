import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { CredentialsService } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

/**
 * A credential's user-group shares and the two writes on them (D5: a
 * separate table from the per-person shares). Removal invalidates the same
 * caches the person path does, plus the deletion impact the disable confirm
 * reads.
 */
export function useCredentialGroupShares(
  credentialId: string,
  enabled: boolean,
) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const query = useQuery({
    queryKey: ["credential-group-shares", credentialId],
    queryFn: () =>
      CredentialsService.listCredentialGroupShares({ credentialId }),
    enabled,
  })

  const invalidate = (groupId: string) => {
    // The admin group pages count and list what is shared with the group.
    queryClient.invalidateQueries({ queryKey: ["user-groups"] })
    queryClient.invalidateQueries({ queryKey: ["user-group", groupId] })
    queryClient.invalidateQueries({
      queryKey: ["credential-group-shares", credentialId],
    })
    queryClient.invalidateQueries({ queryKey: ["credentials"] })
    queryClient.invalidateQueries({ queryKey: ["credential", credentialId] })
    queryClient.invalidateQueries({
      queryKey: ["credential-deletion-impact", credentialId],
    })
  }

  const share = useMutation({
    mutationFn: (group: { id: string; name: string }) =>
      CredentialsService.shareCredentialWithGroup({
        credentialId,
        requestBody: { group_id: group.id },
      }),
    onSuccess: (_, group) => {
      showSuccessToast(`Shared with ${group.name}`)
      invalidate(group.id)
    },
    onError: handleError.bind(showErrorToast),
  })

  const revoke = useMutation({
    mutationFn: (group: { id: string; name: string }) =>
      CredentialsService.revokeCredentialGroupShare({
        credentialId,
        groupId: group.id,
      }),
    onSuccess: (_, group) => {
      showSuccessToast(`Stopped sharing with ${group.name}`)
      invalidate(group.id)
    },
    onError: handleError.bind(showErrorToast),
  })

  return {
    data: query.data,
    groupShares: query.data?.data ?? [],
    isLoading: query.isLoading,
    isError: query.isError,
    error: query.error,
    refetch: query.refetch,
    share,
    revoke,
  }
}
