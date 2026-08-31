import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Pencil, ShieldCheck, Trash2, UserPlus } from "lucide-react"
import { useMemo, useState } from "react"
import type { AgentApiAccessGrantPublic } from "@/client"
import { AgentApiService, AgentsService } from "@/client"
import {
  AgentApiScopeEditor,
  type ScopeCatalogEntry,
} from "@/components/Common/AgentApiScopeEditor"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { Badge } from "@/components/ui/badge"
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
import { Switch } from "@/components/ui/switch"
import useCustomToast from "@/hooks/useCustomToast"

interface AgentApiAccessScopesCardProps {
  agentId: string
  /** Current value of the producer's per-user-scopes opt-in flag. */
  identityEnabled: boolean
}

/**
 * "Access & Scopes" — the producer owner assigns per-user capability scopes on
 * this agent's REST API. The platform resolves the grant live on every call and
 * injects ``X-Cinna-Caller-Scopes`` so the producer can do capability-level
 * authorization in code (the producer reads ``caller.scopes`` via the cinna_api
 * SDK). Available scope names come from the producer's ``policy.yaml`` catalog.
 *
 * Granted users are listed with their scopes; an "Add user" button opens a modal
 * that picks a user and assigns scopes in one step. The same modal edits an
 * existing grant's scopes.
 */
export function AgentApiAccessScopesCard({
  agentId,
  identityEnabled,
}: AgentApiAccessScopesCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Dialog state: closed | add | edit-an-existing-grant.
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editingGrant, setEditingGrant] =
    useState<AgentApiAccessGrantPublic | null>(null)

  const { data: grantsData } = useQuery({
    queryKey: ["agentApiGrants", agentId],
    queryFn: () => AgentApiService.listAgentApiGrants({ agentId }),
    enabled: identityEnabled,
  })
  const grants: AgentApiAccessGrantPublic[] = grantsData?.data ?? []

  const { data: catalogData } = useQuery({
    queryKey: ["agentApiScopeCatalog", agentId],
    queryFn: () => AgentApiService.getAgentApiScopeCatalog({ agentId }),
    enabled: identityEnabled,
  })
  const catalogScopes: ScopeCatalogEntry[] = catalogData?.scopes ?? []

  const invalidateGrants = () =>
    queryClient.invalidateQueries({ queryKey: ["agentApiGrants", agentId] })

  const toggleIdentityMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      AgentsService.updateAgent({
        id: agentId,
        requestBody: { agent_api_identity_enabled: enabled },
      }),
    onSuccess: () => {
      showSuccessToast("Per-user access updated")
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agents"] })
    },
    onError: (e: any) => showErrorToast(e?.message || "Failed to update"),
  })

  const createGrantMutation = useMutation({
    mutationFn: ({ userId, scopes }: { userId: string; scopes: string[] }) =>
      AgentApiService.createAgentApiGrant({
        agentId,
        requestBody: { user_id: userId, scopes },
      }),
    onSuccess: () => {
      showSuccessToast("User added")
      invalidateGrants()
      closeDialog()
    },
    onError: (e: any) => showErrorToast(e?.message || "Failed to add user"),
  })

  const updateScopesMutation = useMutation({
    mutationFn: ({ grantId, scopes }: { grantId: string; scopes: string[] }) =>
      AgentApiService.updateAgentApiGrant({
        agentId,
        grantId,
        requestBody: { scopes },
      }),
    onSuccess: () => {
      showSuccessToast("Scopes updated")
      invalidateGrants()
      closeDialog()
    },
    onError: (e: any) => showErrorToast(e?.message || "Failed to update scopes"),
  })

  const deleteGrantMutation = useMutation({
    mutationFn: (grantId: string) =>
      AgentApiService.deleteAgentApiGrant({ agentId, grantId }),
    onSuccess: () => {
      showSuccessToast("User removed")
      invalidateGrants()
    },
    onError: (e: any) => showErrorToast(e?.message || "Failed to remove user"),
  })

  const grantedUserIds = useMemo(
    () => grants.map((g) => g.user_id),
    [grants],
  )

  const openAddDialog = () => {
    setEditingGrant(null)
    setDialogOpen(true)
  }
  const openEditDialog = (grant: AgentApiAccessGrantPublic) => {
    setEditingGrant(grant)
    setDialogOpen(true)
  }
  const closeDialog = () => {
    setDialogOpen(false)
    setEditingGrant(null)
  }

  const handleSave = (userId: string, scopes: string[]) => {
    if (editingGrant) {
      updateScopesMutation.mutate({ grantId: editingGrant.id, scopes })
    } else {
      createGrantMutation.mutate({ userId, scopes })
    }
  }

  const header = (checked: boolean) => (
    <div className="flex items-start justify-between gap-2">
      <div className="space-y-1">
        <div className="flex items-center gap-2">
          <ShieldCheck className="h-4 w-4 text-muted-foreground" />
          <span className="text-sm font-medium">Access &amp; Scopes</span>
        </div>
        <p className="text-xs text-muted-foreground">
          {checked ? (
            <>
              Assign scopes to platform users. The platform resolves them live on
              every call (effective on the next call) and your API reads them via{" "}
              <code className="text-[11px]">caller.scopes</code>.
            </>
          ) : (
            <>
              Identify calling users and grant each one capability scopes your API
              enforces in code. Off by default — callers are still identified, but
              carry no scopes.
            </>
          )}
        </p>
      </div>
      <Switch
        checked={checked}
        onCheckedChange={(v) => toggleIdentityMutation.mutate(v)}
        disabled={toggleIdentityMutation.isPending}
        className="mt-0.5"
      />
    </div>
  )

  if (!identityEnabled) {
    return (
      <div className="rounded-lg border p-3 space-y-2">{header(false)}</div>
    )
  }

  return (
    <div className="rounded-lg border p-3 space-y-3">
      {header(true)}

      {grants.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No users have been granted access yet. Add one to assign scopes.
        </p>
      ) : (
        <div className="space-y-2">
          {grants.map((grant) => (
            <GrantRow
              key={grant.id}
              grant={grant}
              disabled={deleteGrantMutation.isPending}
              onEdit={() => openEditDialog(grant)}
              onRemove={() => deleteGrantMutation.mutate(grant.id)}
            />
          ))}
        </div>
      )}

      <Button
        variant="outline"
        size="sm"
        className="h-8"
        onClick={openAddDialog}
      >
        <UserPlus className="h-4 w-4 mr-1.5" />
        Add user
      </Button>

      <GrantDialog
        open={dialogOpen}
        onOpenChange={(o) => (o ? setDialogOpen(true) : closeDialog())}
        editingGrant={editingGrant}
        catalogScopes={catalogScopes}
        excludedUserIds={grantedUserIds}
        isSaving={createGrantMutation.isPending || updateScopesMutation.isPending}
        onSave={handleSave}
      />
    </div>
  )
}

interface GrantRowProps {
  grant: AgentApiAccessGrantPublic
  disabled?: boolean
  onEdit: () => void
  onRemove: () => void
}

/** One granted user: name, read-only scope chips, edit + remove actions. */
function GrantRow({ grant, disabled, onEdit, onRemove }: GrantRowProps) {
  const scopes = grant.scopes ?? []
  return (
    <div className="rounded-md border px-3 py-2 flex items-start justify-between gap-2">
      <div className="space-y-1.5 min-w-0">
        <div className="min-w-0">
          <span className="text-xs font-medium block truncate">
            {grant.user?.full_name || grant.user?.email || grant.user_id}
          </span>
          {grant.user?.full_name && grant.user?.email && (
            <span className="text-[11px] text-muted-foreground block truncate">
              {grant.user.email}
            </span>
          )}
        </div>
        <div className="flex flex-wrap gap-1.5">
          {scopes.length === 0 ? (
            <span className="text-xs text-muted-foreground italic">
              No scopes — identified but no capabilities granted.
            </span>
          ) : (
            scopes.map((scope) => (
              <Badge key={scope} variant="secondary" className="text-xs">
                {scope}
              </Badge>
            ))
          )}
        </div>
      </div>
      <div className="flex items-center gap-1 shrink-0">
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={onEdit}
          aria-label="Edit scopes"
        >
          <Pencil className="h-3.5 w-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 text-muted-foreground hover:text-destructive"
          onClick={onRemove}
          disabled={disabled}
          aria-label="Remove user"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  )
}

interface GrantDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** When set, the dialog edits this grant's scopes (user is fixed). */
  editingGrant: AgentApiAccessGrantPublic | null
  catalogScopes: ScopeCatalogEntry[]
  /** User ids already granted — filtered out of the add-mode search. */
  excludedUserIds: string[]
  isSaving?: boolean
  onSave: (userId: string, scopes: string[]) => void
}

/**
 * Modal to add a user with scopes, or edit an existing grant's scopes. In add
 * mode the user picker is shown; in edit mode the user is fixed and only the
 * scope editor is shown.
 */
function GrantDialog({
  open,
  onOpenChange,
  editingGrant,
  catalogScopes,
  excludedUserIds,
  isSaving,
  onSave,
}: GrantDialogProps) {
  const isEdit = !!editingGrant

  // Selected user (add mode only). Edit mode derives the user from the grant.
  const [selectedUser, setSelectedUser] =
    useState<UserAllowlistSelectedItem | null>(null)
  const [scopes, setScopes] = useState<string[]>([])

  // Reset local state whenever the dialog (re)opens, so add starts empty and
  // edit starts from the grant's current scopes. The key tracks `open` too —
  // we must record the closed state so a later close→open transition is
  // detected; otherwise two consecutive "Add user" opens share the same key
  // and the reset never re-fires, leaking the previous record's scopes.
  const resetKey = `${open}:${editingGrant?.id ?? "add"}`
  const [lastResetKey, setLastResetKey] = useState("")
  if (resetKey !== lastResetKey) {
    setLastResetKey(resetKey)
    if (open) {
      setSelectedUser(null)
      setScopes(editingGrant?.scopes ?? [])
    }
  }

  const canSave = isEdit || !!selectedUser
  const handleSave = () => {
    if (isEdit) {
      onSave(editingGrant.user_id, scopes)
    } else if (selectedUser) {
      onSave(selectedUser.userId, scopes)
    }
  }

  const targetLabel = isEdit
    ? editingGrant.user?.full_name && editingGrant.user?.email
      ? `${editingGrant.user.full_name} (${editingGrant.user.email})`
      : editingGrant.user?.full_name ||
        editingGrant.user?.email ||
        editingGrant.user_id
    : null

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit scopes" : "Grant access"}</DialogTitle>
          <DialogDescription>
            {isEdit ? (
              <>
                Scopes for{" "}
                <span className="font-medium text-foreground">
                  {targetLabel}
                </span>
                . Effective on the next call.
              </>
            ) : (
              "Pick a user and assign the capability scopes your API enforces."
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-1">
          {!isEdit && (
            <UserAllowlistPicker
              enabled={open}
              includeSelf
              selected={selectedUser ? [selectedUser] : []}
              excludeUserIds={excludedUserIds}
              label={
                <Label className="text-xs text-muted-foreground">User</Label>
              }
              searchPlaceholder="Search users to grant access..."
              onAdd={(u) =>
                setSelectedUser({
                  id: u.id,
                  userId: u.id,
                  fallbackLabel: u.full_name || u.email,
                })
              }
              onRemove={() => setSelectedUser(null)}
            />
          )}

          <div className="space-y-2">
            <Label className="text-xs text-muted-foreground">Scopes</Label>
            <AgentApiScopeEditor
              scopes={scopes}
              onChange={setScopes}
              catalogScopes={catalogScopes}
            />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={!canSave || isSaving}>
            {isEdit ? "Save scopes" : "Grant access"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
