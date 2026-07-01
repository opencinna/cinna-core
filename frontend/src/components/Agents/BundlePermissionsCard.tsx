import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Lock, Pencil, ShieldCheck, Trash2, UserPlus } from "lucide-react"
import { useMemo, useState } from "react"

import type {
  AgentPublic,
  BundlePermissionProducer,
  BundlePermissionUser,
} from "@/client"
import { AgentApiService, BundlesService, InstallsService } from "@/client"
import {
  BundlePermissionsAddUserModal,
  type FixedUserContext,
} from "@/components/Agents/BundlePermissionsAddUserModal"
import { AgentBadge } from "@/components/Common/AgentBadge"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import useCustomToast from "@/hooks/useCustomToast"

interface BundlePermissionsCardProps {
  agent: AgentPublic
  bundleUuid: string
}

/**
 * "Permissions management" — the unified publisher-facing card on the Bundle
 * tab that replaces the old inline bundle-grants picker. One compact row per
 * user (mirroring the Revisions card row shape); membership in the list *is*
 * bundle access, so there is no separate on/off toggle. Each row surfaces a
 * scope-chip cluster per manageable connected producer and a single remove
 * action.
 *
 * The remove action cascades across BOTH authority domains in one confirmed
 * step: it revokes the user's bundle grant (if any) AND every producer-scope
 * grant this user holds across all manageable producers. Producers the
 * publisher does NOT own render only as a header note — their grants are
 * genuinely unreadable (the owner-gated read never ran).
 */
export function BundlePermissionsCard({
  agent,
  bundleUuid,
}: BundlePermissionsCardProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [addOpen, setAddOpen] = useState(false)
  const [editUser, setEditUser] = useState<FixedUserContext | null>(null)
  const [removeTarget, setRemoveTarget] = useState<BundlePermissionUser | null>(
    null,
  )

  const { data: overview, isLoading } = useQuery({
    queryKey: ["bundlePermissionsOverview", agent.id],
    queryFn: () =>
      InstallsService.getBundlePermissionsOverview({ agentId: agent.id }),
    enabled: agent.is_publisher_install && !!agent.bundle_uuid,
  })

  const invalidateOverview = () =>
    queryClient.invalidateQueries({
      queryKey: ["bundlePermissionsOverview", agent.id],
    })

  const producers = overview?.producers ?? []
  const users = overview?.users ?? []
  const bundleAccessApplicable = overview?.bundle_access_applicable ?? false

  const manageableProducers = useMemo(
    () => producers.filter((p) => p.can_manage),
    [producers],
  )
  const unmanageableProducers = useMemo(
    () => producers.filter((p) => !p.can_manage),
    [producers],
  )

  const excludeUserIds = useMemo(() => users.map((u) => u.user_id), [users])
  const canAddAnything = bundleAccessApplicable || manageableProducers.length > 0

  /**
   * Cascading, multi-domain removal of a user's entire access record: the
   * bundle grant (if present) plus every producer-scope grant this user holds
   * across all manageable producers. Fans out over the existing per-domain
   * endpoints via ``Promise.allSettled`` so a partial failure still removes
   * what it can — the overview re-query is the source of truth afterwards.
   */
  const removeUserMutation = useMutation({
    mutationFn: async (user: BundlePermissionUser) => {
      const tasks: {
        label: string
        producerId?: string
        run: () => Promise<unknown>
      }[] = []

      if (user.bundle_grant_id) {
        tasks.push({
          label: "bundle access",
          run: () =>
            BundlesService.revokeGrant({
              bundleUuid,
              grantId: user.bundle_grant_id as string,
            }),
        })
      }

      for (const producer of manageableProducers) {
        const grant = producer.grants?.find((g) => g.user_id === user.user_id)
        if (grant) {
          tasks.push({
            label: producer.producer_agent_name || "producer scopes",
            producerId: producer.producer_agent_id,
            run: () =>
              AgentApiService.deleteAgentApiGrant({
                agentId: producer.producer_agent_id,
                grantId: grant.grant_id,
              }),
          })
        }
      }

      const results = await Promise.allSettled(tasks.map((t) => t.run()))
      const touchedProducerIds = new Set<string>()
      const failures: string[] = []
      results.forEach((result, i) => {
        if (result.status === "fulfilled") {
          if (tasks[i].producerId)
            touchedProducerIds.add(tasks[i].producerId as string)
        } else {
          failures.push(tasks[i].label)
        }
      })
      return { failures, touchedProducerIds }
    },
    onSuccess: ({ failures, touchedProducerIds }) => {
      invalidateOverview()
      queryClient.invalidateQueries({
        queryKey: ["bundles", bundleUuid, "grants"],
      })
      for (const producerId of touchedProducerIds) {
        queryClient.invalidateQueries({
          queryKey: ["agentApiGrants", producerId],
        })
      }
      if (failures.length > 0) {
        showErrorToast(
          `Removed with issues — could not remove: ${failures.join(", ")}`,
        )
      } else {
        showSuccessToast("User access removed")
      }
      setRemoveTarget(null)
    },
    onError: (e: any) => {
      invalidateOverview()
      showErrorToast(e?.body?.detail || "Failed to remove user access")
      setRemoveTarget(null)
    },
  })

  if (isLoading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Permissions management</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">Loading…</p>
        </CardContent>
      </Card>
    )
  }
  if (!overview || !overview.show_card) return null

  const openEditForUser = (user: BundlePermissionUser) =>
    setEditUser({
      userId: user.user_id,
      email: user.email,
      fullName: user.full_name,
    })

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="space-y-1.5 min-w-0">
            <CardTitle className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-muted-foreground" />
              Permissions management
            </CardTitle>
            <CardDescription>
              Everyone listed here can access this bundle. Manage what each user
              may do on its connected producer APIs, all in one place.
            </CardDescription>
          </div>
          {canAddAnything && (
            <Button size="sm" onClick={() => setAddOpen(true)}>
              <UserPlus className="h-4 w-4 mr-1.5" />
              Add user
            </Button>
          )}
        </div>

        {/* Producers the publisher does NOT own are surfaced once here — their
            per-user scopes live on their own page and are unreadable here. */}
        {unmanageableProducers.length > 0 && (
          <div className="mt-2 space-y-1 rounded-md border border-dashed p-2.5">
            {unmanageableProducers.map((producer) => (
              <div
                key={producer.producer_agent_id}
                className="flex items-center gap-1.5 text-[11px] text-muted-foreground"
              >
                <Lock className="h-3 w-3 shrink-0" />
                <span>
                  Also connected:{" "}
                  <span className="font-medium">
                    {producer.producer_agent_name || "producer"}
                  </span>{" "}
                  — managed by {producer.owner_email || "another owner"} on its
                  own page
                </span>
              </div>
            ))}
          </div>
        )}
      </CardHeader>

      <CardContent>
        {users.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {bundleAccessApplicable
              ? "No users yet. Add a user to grant bundle access and assign producer scopes."
              : "No users yet. Add a user to assign their producer scopes."}
          </p>
        ) : (
          <div className="space-y-1.5">
            {users.map((user) => (
              <div
                key={user.user_id}
                className="flex items-center justify-between gap-3 px-3 py-2 border rounded-lg"
              >
                {/* Left — user identity. */}
                <div className="min-w-0">
                  <span className="text-sm font-medium block truncate">
                    {user.full_name || user.email || user.user_id}
                  </span>
                  {user.full_name && user.email && (
                    <span className="text-[11px] text-muted-foreground block truncate">
                      {user.email}
                    </span>
                  )}
                </div>

                {/* Right — per-manageable-producer scope clusters + remove. */}
                <div className="flex items-center gap-2 shrink-0">
                  {manageableProducers.map((producer) => (
                    <ProducerScopeInline
                      key={producer.producer_agent_id}
                      producer={producer}
                      user={user}
                      onEdit={() => openEditForUser(user)}
                    />
                  ))}
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={() => openEditForUser(user)}
                    aria-label="Edit user permissions"
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 text-destructive hover:text-destructive"
                    onClick={() => setRemoveTarget(user)}
                    aria-label="Remove user access"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>

      {/* Add user — fresh modal. */}
      <BundlePermissionsAddUserModal
        open={addOpen}
        onOpenChange={setAddOpen}
        agentId={agent.id}
        bundleUuid={bundleUuid}
        bundleAccessApplicable={bundleAccessApplicable}
        producers={producers}
        excludeUserIds={excludeUserIds}
      />

      {/* Edit an existing user (producer-cluster click). */}
      <BundlePermissionsAddUserModal
        open={!!editUser}
        onOpenChange={(o) => {
          if (!o) setEditUser(null)
        }}
        agentId={agent.id}
        bundleUuid={bundleUuid}
        bundleAccessApplicable={bundleAccessApplicable}
        producers={producers}
        excludeUserIds={excludeUserIds}
        fixedUser={editUser}
      />

      {/* Cascading remove confirmation — mirrors the Delete revision dialog. */}
      <AlertDialog
        open={!!removeTarget}
        onOpenChange={(open) => {
          if (!open) setRemoveTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Remove{" "}
              {removeTarget?.full_name ||
                removeTarget?.email ||
                "this user"}
              ?
            </AlertDialogTitle>
            <AlertDialogDescription>
              This removes their entire access record for this bundle — the
              bundle grant and every producer-scope grant they hold across the
              producers you manage. Producers managed by another owner are not
              affected. This cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={removeUserMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                if (removeTarget) removeUserMutation.mutate(removeTarget)
              }}
              disabled={removeUserMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {removeUserMutation.isPending ? "Removing..." : "Remove access"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  )
}

interface ProducerScopeInlineProps {
  producer: BundlePermissionProducer
  user: BundlePermissionUser
  onEdit: () => void
}

/**
 * Inline scope-chip cluster for one manageable producer on one user's row.
 * Clicking opens the edit modal for that user. Never rendered for
 * non-manageable producers (their per-user data is unavailable).
 */
function ProducerScopeInline({
  producer,
  user,
  onEdit,
}: ProducerScopeInlineProps) {
  const grant = producer.grants?.find((g) => g.user_id === user.user_id)
  const scopes = grant?.scopes ?? []

  return (
    <button
      type="button"
      onClick={onEdit}
      className="flex items-center gap-1.5 rounded-md border px-2 py-1 text-left transition-colors hover:bg-accent max-w-[280px]"
      title={`Edit ${producer.producer_agent_name || "producer"} scopes`}
    >
      <AgentBadge
        agent={{
          id: producer.producer_agent_id,
          name: producer.producer_agent_name || "Producer",
          ui_color_preset: producer.producer_ui_color_preset,
        }}
        linkTo="none"
      />
      {!grant ? (
        <span className="text-[11px] text-muted-foreground">
          + assign scopes
        </span>
      ) : scopes.length === 0 ? (
        <span className="text-[11px] text-muted-foreground italic">
          no scopes
        </span>
      ) : (
        <span className="flex flex-wrap gap-1">
          {scopes.map((scope) => (
            <Badge key={scope} variant="secondary" className="text-[10px]">
              {scope}
            </Badge>
          ))}
        </span>
      )}
    </button>
  )
}
