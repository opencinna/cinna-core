import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Plus, ShieldCheck, X } from "lucide-react"
import { useMemo, useState } from "react"
import type { AgentApiAccessGrantPublic } from "@/client"
import { AgentApiService, AgentsService } from "@/client"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
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
 * Mirrors the MCP connector ACL card: ``UserAllowlistPicker`` + GET /users/search
 * to pick users, then per-user scope assignment.
 */
export function AgentApiAccessScopesCard({
  agentId,
  identityEnabled,
}: AgentApiAccessScopesCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

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
  const catalogScopes = catalogData?.scopes ?? []

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
    mutationFn: (userId: string) =>
      AgentApiService.createAgentApiGrant({
        agentId,
        requestBody: { user_id: userId, scopes: [] },
      }),
    onSuccess: () => {
      showSuccessToast("User added")
      invalidateGrants()
    },
    onError: (e: any) => showErrorToast(e?.message || "Failed to add user"),
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

  const updateScopesMutation = useMutation({
    mutationFn: ({ grantId, scopes }: { grantId: string; scopes: string[] }) =>
      AgentApiService.updateAgentApiGrant({
        agentId,
        grantId,
        requestBody: { scopes },
      }),
    onSuccess: () => invalidateGrants(),
    onError: (e: any) => showErrorToast(e?.message || "Failed to update scopes"),
  })

  const selectedUsers: UserAllowlistSelectedItem[] = useMemo(
    () =>
      grants.map((g) => ({
        id: g.id,
        userId: g.user_id,
        fallbackLabel: g.user?.full_name || g.user?.email || g.user_id,
      })),
    [grants],
  )

  if (!identityEnabled) {
    return (
      <div className="rounded-lg border p-3 space-y-2">
        <div className="flex items-start justify-between gap-2">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-muted-foreground" />
              <span className="text-sm font-medium">Access &amp; Scopes</span>
            </div>
            <p className="text-xs text-muted-foreground">
              Identify calling users and grant each one capability scopes your
              API enforces in code. Off by default — callers are still
              identified, but carry no scopes.
            </p>
          </div>
          <Switch
            checked={false}
            onCheckedChange={(v) => toggleIdentityMutation.mutate(v)}
            disabled={toggleIdentityMutation.isPending}
            className="mt-0.5"
          />
        </div>
      </div>
    )
  }

  return (
    <div className="rounded-lg border p-3 space-y-3">
      <div className="flex items-start justify-between gap-2">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm font-medium">Access &amp; Scopes</span>
          </div>
          <p className="text-xs text-muted-foreground">
            Assign scopes to platform users. The platform resolves them live on
            every call (effective on the next call) and your API reads them via{" "}
            <code className="text-[11px]">caller.scopes</code>.
          </p>
        </div>
        <Switch
          checked
          onCheckedChange={(v) => toggleIdentityMutation.mutate(v)}
          disabled={toggleIdentityMutation.isPending}
          className="mt-0.5"
        />
      </div>

      <UserAllowlistPicker
        selected={selectedUsers}
        label={
          <Label className="text-xs text-muted-foreground">Granted users</Label>
        }
        searchPlaceholder="Search users to grant access..."
        emptyHint="No users have been granted access yet. Search above to add one."
        isAdding={createGrantMutation.isPending}
        isRemoving={deleteGrantMutation.isPending}
        onAdd={(u) => createGrantMutation.mutate(u.id)}
        onRemove={(item) => deleteGrantMutation.mutate(item.id)}
      />

      {grants.length > 0 && (
        <div className="space-y-2">
          {grants.map((grant) => (
            <GrantScopeRow
              key={grant.id}
              grant={grant}
              catalogScopes={catalogScopes}
              disabled={updateScopesMutation.isPending}
              onChangeScopes={(scopes) =>
                updateScopesMutation.mutate({ grantId: grant.id, scopes })
              }
            />
          ))}
        </div>
      )}
    </div>
  )
}

interface GrantScopeRowProps {
  grant: AgentApiAccessGrantPublic
  catalogScopes: { name: string; description?: string | null }[]
  disabled?: boolean
  onChangeScopes: (scopes: string[]) => void
}

/** One granted user with their scope chips + an add control. */
function GrantScopeRow({
  grant,
  catalogScopes,
  disabled,
  onChangeScopes,
}: GrantScopeRowProps) {
  const [customScope, setCustomScope] = useState("")
  const current = grant.scopes ?? []

  const toggle = (scope: string) => {
    const next = current.includes(scope)
      ? current.filter((s) => s !== scope)
      : [...current, scope]
    onChangeScopes(next)
  }

  const addCustom = () => {
    const value = customScope.trim()
    if (!value || current.includes(value)) {
      setCustomScope("")
      return
    }
    onChangeScopes([...current, value])
    setCustomScope("")
  }

  // Catalog scopes not yet assigned are offered as quick-add suggestions.
  const unassignedCatalog = catalogScopes.filter(
    (s) => !current.includes(s.name),
  )

  return (
    <div className="rounded-md border px-3 py-2 space-y-2">
      <span className="text-xs font-medium">
        {grant.user?.full_name || grant.user?.email || grant.user_id}
      </span>

      {/* Assigned scopes (removable chips) */}
      <div className="flex flex-wrap gap-1.5">
        {current.length === 0 ? (
          <span className="text-xs text-muted-foreground italic">
            No scopes — identified but no capabilities granted.
          </span>
        ) : (
          current.map((scope) => (
            <Badge key={scope} variant="secondary" className="gap-1 text-xs">
              {scope}
              <button
                type="button"
                onClick={() => toggle(scope)}
                disabled={disabled}
                className="hover:text-destructive transition-colors"
                aria-label={`Remove scope ${scope}`}
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          ))
        )}
      </div>

      {/* Quick-add from the policy.yaml catalog */}
      {unassignedCatalog.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {unassignedCatalog.map((s) => (
            <button
              key={s.name}
              type="button"
              onClick={() => toggle(s.name)}
              disabled={disabled}
              title={s.description ?? undefined}
              className="inline-flex items-center gap-1 rounded-full border border-dashed px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent transition-colors"
            >
              <Plus className="h-3 w-3" />
              {s.name}
            </button>
          ))}
        </div>
      )}

      {/* Free-text scope add (catalog may be empty until the producer declares
          scopes in policy.yaml). */}
      <div className="flex items-center gap-1.5">
        <Input
          value={customScope}
          onChange={(e) => setCustomScope(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault()
              addCustom()
            }
          }}
          placeholder="Add a scope name..."
          className="h-7 text-xs"
          disabled={disabled}
        />
        <Button
          variant="outline"
          size="sm"
          className="h-7 shrink-0"
          onClick={addCustom}
          disabled={disabled || !customScope.trim()}
        >
          Add
        </Button>
      </div>
    </div>
  )
}
