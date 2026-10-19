import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Shield } from "lucide-react"

import type {
  ApiError,
  KnowledgeSourceAccessLevel,
  AIKnowledgeGitRepoPublic as KnowledgeSourceRead,
} from "@/client"
import { KnowledgeSourcesService } from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import {
  type GroupSelectedItem,
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

import { ACCESS_LEVEL_OPTIONS, AccessLevelRadioGroup } from "./accessLevel"

interface KnowledgeSourceAccessCardProps {
  source: KnowledgeSourceRead
  sourceId: string
}

export function KnowledgeSourceAccessCard({
  source,
  sourceId,
}: KnowledgeSourceAccessCardProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const queryClient = useQueryClient()
  const accessLevel: KnowledgeSourceAccessLevel =
    source.access_level || "private"
  const isShared = accessLevel === "shared"
  // Under the source's own key so every `["knowledge-source", sourceId]`
  // invalidation refreshes the share list too.
  const sharedUsersKey = ["knowledge-source", sourceId, "shared-users"]
  const sharedGroupsKey = ["knowledge-source", sourceId, "shared-groups"]

  const invalidateSource = () => {
    queryClient.invalidateQueries({ queryKey: ["knowledge-source", sourceId] })
    queryClient.invalidateQueries({ queryKey: ["knowledge-sources"] })
  }

  // The admin group pages count and list what is shared with the group.
  const invalidateGroup = (groupId: string) => {
    queryClient.invalidateQueries({ queryKey: ["user-groups"] })
    queryClient.invalidateQueries({ queryKey: ["user-group", groupId] })
  }

  const accessLevelMutation = useMutation({
    mutationFn: (level: KnowledgeSourceAccessLevel) =>
      KnowledgeSourcesService.updateKnowledgeSource({
        sourceId,
        requestBody: { access_level: level },
      }),
    onSuccess: (_, level) => {
      const option = ACCESS_LEVEL_OPTIONS.find((o) => o.value === level)
      showSuccessToast(
        option
          ? `Access set to ${option.label}: ${option.description.toLowerCase()}`
          : "Access updated",
      )
      invalidateSource()
    },
    onError: (error) => handleError.bind(showErrorToast)(error as ApiError),
  })

  const {
    data: sharedUsers,
    isLoading: isLoadingSharedUsers,
    isError: isSharedUsersError,
    error: sharedUsersError,
    refetch: refetchSharedUsers,
  } = useQuery({
    queryKey: sharedUsersKey,
    queryFn: () =>
      KnowledgeSourcesService.listKnowledgeSourceSharedUsers({ sourceId }),
    enabled: isShared,
  })

  const {
    data: sharedGroups,
    isLoading: isLoadingSharedGroups,
    isError: isSharedGroupsError,
    error: sharedGroupsError,
    refetch: refetchSharedGroups,
  } = useQuery({
    queryKey: sharedGroupsKey,
    queryFn: () =>
      KnowledgeSourcesService.listKnowledgeSourceSharedGroups({ sourceId }),
    enabled: isShared,
  })

  const addGroupMutation = useMutation({
    mutationFn: (groupId: string) =>
      KnowledgeSourcesService.addKnowledgeSourceSharedGroup({
        sourceId,
        requestBody: { group_id: groupId },
      }),
    onSuccess: (share) => {
      showSuccessToast(`Shared with ${share.name}`)
      invalidateSource()
      invalidateGroup(share.group_id)
    },
    onError: (error) => handleError.bind(showErrorToast)(error as ApiError),
  })

  const removeGroupMutation = useMutation({
    mutationFn: (group: { id: string; name: string }) =>
      KnowledgeSourcesService.removeKnowledgeSourceSharedGroup({
        sourceId,
        groupId: group.id,
      }),
    onSuccess: (_, group) => {
      showSuccessToast(`${group.name} removed from the share list`)
      invalidateSource()
      invalidateGroup(group.id)
    },
    onError: (error) => handleError.bind(showErrorToast)(error as ApiError),
  })

  const addUserMutation = useMutation({
    mutationFn: (userId: string) =>
      KnowledgeSourcesService.addKnowledgeSourceSharedUser({
        sourceId,
        requestBody: { user_id: userId },
      }),
    onSuccess: (share) => {
      showSuccessToast(`Shared with ${share.full_name || share.email}`)
      invalidateSource()
    },
    onError: (error) => handleError.bind(showErrorToast)(error as ApiError),
  })

  const removeUserMutation = useMutation({
    mutationFn: (userId: string) =>
      KnowledgeSourcesService.removeKnowledgeSourceSharedUser({
        sourceId,
        userId,
      }),
    onSuccess: () => {
      showSuccessToast("User removed from the share list")
      invalidateSource()
    },
    onError: (error) => handleError.bind(showErrorToast)(error as ApiError),
  })

  const selected: UserAllowlistSelectedItem[] = (sharedUsers ?? []).map(
    (u) => ({
      id: u.user_id,
      userId: u.user_id,
      fallbackLabel: u.full_name || u.email,
    }),
  )

  const selectedGroups: GroupSelectedItem[] = (sharedGroups ?? []).map((g) => ({
    id: g.group_id,
    groupId: g.group_id,
    name: g.name,
    memberCount: g.member_count,
  }))

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Shield className="h-5 w-5" />
          Access
        </CardTitle>
        <CardDescription>
          Who can query this source through agents and knowledge search. Admins
          always have access.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <AccessLevelRadioGroup
          idPrefix="source-access"
          value={accessLevel}
          onChange={(level) => {
            if (level !== accessLevel) accessLevelMutation.mutate(level)
          }}
          disabled={accessLevelMutation.isPending}
        />

        {isShared && (
          <div className="space-y-2 border-t pt-4">
            <Label className="text-sm font-medium">Shared with</Label>
            {isSharedUsersError || isSharedGroupsError ? (
              <QueryErrorAlert
                error={sharedUsersError ?? sharedGroupsError}
                fallback="Couldn't load the share list"
                onRetry={() => {
                  refetchSharedUsers()
                  refetchSharedGroups()
                }}
                compact
              />
            ) : isLoadingSharedUsers || isLoadingSharedGroups ? (
              <div className="space-y-2">
                <div className="flex flex-wrap gap-1.5">
                  <Skeleton className="h-6 w-24 rounded-full" />
                  <Skeleton className="h-6 w-32 rounded-full" />
                </div>
                <Skeleton className="h-9 w-full" />
              </div>
            ) : (
              <UserAllowlistPicker
                label={null}
                selected={selected}
                onAdd={(user) => addUserMutation.mutate(user.id)}
                onRemove={(item) => removeUserMutation.mutate(item.userId)}
                isAdding={addUserMutation.isPending}
                isRemoving={removeUserMutation.isPending}
                groups={{
                  selected: selectedGroups,
                  onAdd: (group) => addGroupMutation.mutate(group.id),
                  onRemove: (item) =>
                    removeGroupMutation.mutate({
                      id: item.groupId,
                      name: item.name,
                    }),
                  isAdding: addGroupMutation.isPending,
                  isRemoving: removeGroupMutation.isPending,
                }}
                searchPlaceholder="Search people or groups to share with..."
                emptyHint="No people or groups yet. Until you add some, only admins can query this source."
              />
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
