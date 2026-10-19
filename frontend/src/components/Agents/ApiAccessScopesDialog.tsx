import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import type { AgentApiAccessEntry, AgentApiAccessGroupEntry } from "@/client"
import { AgentApiService } from "@/client"
import { invalidateUserGroupShareLists } from "@/components/Admin/UserGroups/userGroupQueries"
import { BundlePermissionsTargetPicker } from "@/components/Agents/BundlePermissions/BundlePermissionsTargetPicker"
import {
  AgentApiScopeEditor,
  SCOPE_EMPTY_HINTS,
} from "@/components/Common/AgentApiScopeEditor"
import type {
  GroupSelectedItem,
  UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import { agentApiAccessQueryKey } from "@/hooks/useAgentApiKeys"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { apiAccessPersonLabel } from "./apiAccess"

/** Whose scopes Edit mode changes: one person's grant, or one group's. */
export type ApiAccessScopesTarget =
  | { kind: "person"; entry: AgentApiAccessEntry }
  | { kind: "group"; group: AgentApiAccessGroupEntry }

interface ApiAccessScopesDialogProps {
  agentId: string
  open: boolean
  onOpenChange: (open: boolean) => void
  /**
   * The person or group whose scopes are edited; `null` = grant mode (pick a
   * person or a group — for callers through their own agents' connections,
   * no key).
   */
  target: ApiAccessScopesTarget | null
  /** People who already have a grant — left out of grant mode's search. */
  excludedUserIds: string[]
  /** Groups that already have a grant — left out of grant mode's search. */
  excludedGroupIds: string[]
}

function targetName(target: ApiAccessScopesTarget): string {
  return target.kind === "group"
    ? target.group.name
    : apiAccessPersonLabel(target.entry)
}

function targetKey(target: ApiAccessScopesTarget | null): string {
  if (!target) return "grant"
  return target.kind === "group"
    ? `group:${target.group.group_id}`
    : `person:${target.entry.user.id}`
}

/**
 * S1b — Edit scopes / Grant scopes. A person's scopes live on their grant on
 * this producer, so they apply to every key and connection that person uses
 * here, from the next call; a group's scopes add to each active member's own.
 * Edit on a person with no grant yet creates it.
 */
export function ApiAccessScopesDialog({
  agentId,
  open,
  onOpenChange,
  target,
  excludedUserIds,
  excludedGroupIds,
}: ApiAccessScopesDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const isEdit = !!target

  const [selectedUser, setSelectedUser] =
    useState<UserAllowlistSelectedItem | null>(null)
  const [selectedGroup, setSelectedGroup] = useState<GroupSelectedItem | null>(
    null,
  )
  const [scopes, setScopes] = useState<string[]>([])

  // Reset whenever the dialog (re)opens or switches target. The key tracks
  // `open` too, so two consecutive opens on the same target still re-seed.
  const resetKey = `${open}:${targetKey(target)}`
  const [lastResetKey, setLastResetKey] = useState("")
  if (resetKey !== lastResetKey) {
    setLastResetKey(resetKey)
    if (open) {
      setSelectedUser(null)
      setSelectedGroup(null)
      setScopes(
        (target?.kind === "group"
          ? target.group.scopes
          : target?.entry.scopes) ?? [],
      )
    }
  }

  const { data: catalogData } = useQuery({
    queryKey: ["agentApiScopeCatalog", agentId],
    queryFn: () => AgentApiService.getAgentApiScopeCatalog({ agentId }),
    enabled: open,
  })

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (target?.kind === "group") {
        return AgentApiService.updateAgentApiGroupGrant({
          agentId,
          grantId: target.group.grant_id,
          requestBody: { scopes },
        })
      }
      if (target?.entry.grant_id) {
        return AgentApiService.updateAgentApiGrant({
          agentId,
          grantId: target.entry.grant_id,
          requestBody: { scopes },
        })
      }
      if (!target && selectedGroup) {
        return AgentApiService.createAgentApiGroupGrant({
          agentId,
          requestBody: { group_id: selectedGroup.groupId, scopes },
        })
      }
      const userId = target?.entry.user.id ?? selectedUser?.userId
      return AgentApiService.createAgentApiGrant({
        agentId,
        requestBody: { user_id: userId as string, scopes },
      })
    },
    onSuccess: () => {
      showSuccessToast(isEdit ? "Scopes updated" : "Scopes granted")
      onOpenChange(false)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({
        queryKey: agentApiAccessQueryKey(agentId),
      })
      // A group's grant also shows on the Bundle Permissions card of every
      // bundle consuming this producer.
      queryClient.invalidateQueries({ queryKey: ["bundlePermissionsOverview"] })
      if (target?.kind === "group" || (!target && selectedGroup))
        invalidateUserGroupShareLists(queryClient)
    },
  })

  const name = target ? targetName(target) : ""
  const canSave =
    (isEdit || !!selectedUser || !!selectedGroup) && !saveMutation.isPending

  let description: string
  if (target?.kind === "group") {
    description = `Scopes for ${name}. They add to each member's own scopes on this agent, from the next call.`
  } else if (target) {
    description = `Scopes for ${name}. They apply to every key and connection ${name} uses on this agent, from the next call.`
  } else {
    description =
      "Pick a person or a group and the scopes your API enforces for them. A group's scopes add to each member's own. They apply from the next call."
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!saveMutation.isPending) onOpenChange(next)
      }}
    >
      <DialogContent className="max-h-[85vh] overflow-y-auto overflow-x-hidden sm:max-w-md [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit scopes" : "Grant scopes"}</DialogTitle>
          <DialogDescription className="break-words">
            {description}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {!isEdit && (
            <BundlePermissionsTargetPicker
              enabled={open}
              groupsOffered
              includeSelf
              // Plain, like the Scopes label under it.
              labelClassName=""
              excludeUserIds={excludedUserIds}
              excludeGroupIds={excludedGroupIds}
              selectedUser={selectedUser}
              selectedGroup={selectedGroup}
              onUserChange={(user) => setSelectedUser(user)}
              onGroupChange={setSelectedGroup}
            />
          )}

          <div className="space-y-1.5">
            <Label>Scopes</Label>
            <AgentApiScopeEditor
              scopes={scopes}
              onChange={setScopes}
              catalogScopes={catalogData?.scopes ?? []}
              // Saving here always writes a grant, even an empty one, so the
              // person or the group's members are identified either way.
              emptyHint={
                target?.kind === "group" || (!target && selectedGroup)
                  ? SCOPE_EMPTY_HINTS.group
                  : SCOPE_EMPTY_HINTS.person
              }
              disabled={saveMutation.isPending}
            />
          </div>
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={saveMutation.isPending}
          >
            Cancel
          </Button>
          <LoadingButton
            loading={saveMutation.isPending}
            disabled={!canSave}
            onClick={() => saveMutation.mutate()}
          >
            {isEdit ? "Save scopes" : "Grant scopes"}
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
