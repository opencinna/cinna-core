import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import type { AgentApiAccessEntry } from "@/client"
import { AgentApiService } from "@/client"
import { AgentApiScopeEditor } from "@/components/Common/AgentApiScopeEditor"
import {
  UserSelectField,
  type UserSelectValue,
} from "@/components/Common/UserSelectField"
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

interface ApiAccessScopesDialogProps {
  agentId: string
  open: boolean
  onOpenChange: (open: boolean) => void
  /**
   * The person whose scopes are edited; `null` = grant mode (pick a person,
   * for someone who calls through their own agents' connections, no key).
   */
  person: AgentApiAccessEntry | null
  /** People who already have a grant — left out of grant mode's search. */
  excludedUserIds: string[]
}

/**
 * S1b — Edit scopes / Grant scopes. The scopes live on the person's grant on
 * this producer, so they apply to every key and connection that person uses
 * here, from the next call. Edit with no grant yet creates it.
 */
export function ApiAccessScopesDialog({
  agentId,
  open,
  onOpenChange,
  person,
  excludedUserIds,
}: ApiAccessScopesDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const isEdit = !!person

  const [selectedUser, setSelectedUser] =
    useState<UserSelectValue | null>(null)
  const [scopes, setScopes] = useState<string[]>([])

  // Reset whenever the dialog (re)opens or switches person. The key tracks
  // `open` too, so two consecutive opens on the same person still re-seed.
  const resetKey = `${open}:${person?.user.id ?? "grant"}`
  const [lastResetKey, setLastResetKey] = useState("")
  if (resetKey !== lastResetKey) {
    setLastResetKey(resetKey)
    if (open) {
      setSelectedUser(null)
      setScopes(person?.scopes ?? [])
    }
  }

  const { data: catalogData } = useQuery({
    queryKey: ["agentApiScopeCatalog", agentId],
    queryFn: () => AgentApiService.getAgentApiScopeCatalog({ agentId }),
    enabled: open,
  })

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (person?.grant_id) {
        return AgentApiService.updateAgentApiGrant({
          agentId,
          grantId: person.grant_id,
          requestBody: { scopes },
        })
      }
      const userId = person?.user.id ?? selectedUser?.userId
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
      queryClient.invalidateQueries({ queryKey: agentApiAccessQueryKey(agentId) })
    },
  })

  const name = person ? apiAccessPersonLabel(person) : ""
  const canSave = (isEdit || !!selectedUser) && !saveMutation.isPending

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
            {isEdit
              ? `Scopes for ${name}. They apply to every key and connection ${name} uses on this agent, from the next call.`
              : "Pick a person and the scopes your API enforces for them. They apply from the next call."}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {!isEdit && (
            <div className="space-y-1.5">
              <Label htmlFor="api-access-grant-person">Person</Label>
              <UserSelectField
                id="api-access-grant-person"
                value={selectedUser}
                onChange={setSelectedUser}
                includeSelf
                excludeUserIds={excludedUserIds}
                disabled={saveMutation.isPending}
              />
            </div>
          )}

          <div className="space-y-1.5">
            <Label>Scopes</Label>
            <AgentApiScopeEditor
              scopes={scopes}
              onChange={setScopes}
              catalogScopes={catalogData?.scopes ?? []}
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
