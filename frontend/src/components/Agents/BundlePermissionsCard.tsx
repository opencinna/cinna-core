import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Lock, ShieldCheck, UserPlus } from "lucide-react"
import { useMemo, useState } from "react"

import type {
  AgentPublic,
  BundlePermissionGroup,
  BundlePermissionUser,
} from "@/client"
import { AgentApiService, BundlesService, InstallsService } from "@/client"
import { BundlePermissionGroups } from "@/components/Agents/BundlePermissions/BundlePermissionGroups"
import { BundlePermissionPeople } from "@/components/Agents/BundlePermissions/BundlePermissionPeople"
import {
  existingGrantFor,
  type PermissionTarget,
} from "@/components/Agents/BundlePermissions/bundlePermissionsActions"
import { BundlePermissionsAddUserModal } from "@/components/Agents/BundlePermissionsAddUserModal"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
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
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { agentApiAccessQueryKey } from "@/hooks/useAgentApiKeys"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"

interface BundlePermissionsCardProps {
  agent: AgentPublic
  bundleUuid: string
}

/**
 * "Permissions management" — the unified publisher-facing card on the Bundle
 * tab that replaces the old inline bundle-grants picker. One compact row per
 * person, then one per user group (mirroring the Revisions card row shape);
 * membership in the list *is* bundle access, so there is no separate on/off
 * toggle and no level. Each row surfaces a scope-chip cluster per manageable
 * connected producer and an Edit / Remove menu.
 *
 * The remove action cascades across BOTH authority domains in one confirmed
 * step: it revokes the row's bundle grant (if any) AND every producer-scope
 * grant it holds across all manageable producers. Producers the
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
  const [editTarget, setEditTarget] = useState<PermissionTarget | null>(null)
  const [removeTarget, setRemoveTarget] = useState<BundlePermissionUser | null>(
    null,
  )

  const {
    data: overview,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
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
  const groups = overview?.groups ?? []
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
  const excludeGroupIds = useMemo(() => groups.map((g) => g.group_id), [groups])
  const canAddAnything =
    bundleAccessApplicable || manageableProducers.length > 0

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
        const grant = existingGrantFor(producer, user.user_id)
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
          queryKey: agentApiAccessQueryKey(producerId),
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
    onError: (e) => {
      invalidateOverview()
      showErrorToast(getErrorMessage(e, "Failed to remove user access"))
      setRemoveTarget(null)
    },
  })

  // Loading and failure keep the card in its grid slot: a failed fetch that
  // returned `null` here would make the card vanish without a word.
  if (isLoading || isError) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 min-w-0">
            <ShieldCheck className="h-5 w-5" />
            Permissions management
          </CardTitle>
        </CardHeader>
        <CardContent>
          {isError ? (
            <QueryErrorAlert
              error={error}
              fallback="Couldn't load bundle permissions."
              onRetry={() => refetch()}
            />
          ) : (
            <div className="space-y-1.5">
              {Array.from({ length: 3 }, (_, i) => (
                <Skeleton key={i} className="h-[48px] w-full rounded-md" />
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    )
  }
  if (!overview || !overview.show_card) return null

  const openEditForUser = (user: BundlePermissionUser) =>
    setEditTarget({
      kind: "user",
      id: user.user_id,
      email: user.email,
      fullName: user.full_name,
    })
  const openEditForGroup = (group: BundlePermissionGroup) =>
    setEditTarget({ kind: "group", id: group.group_id, name: group.name })

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 min-w-0">
            <ShieldCheck className="h-5 w-5 shrink-0" />
            Permissions management
          </CardTitle>
          {canAddAnything && (
            <Button size="sm" onClick={() => setAddOpen(true)}>
              <UserPlus className="h-4 w-4 mr-1.5" />
              Add access
            </Button>
          )}
        </div>
        <CardDescription>
          Everyone listed here can access this bundle. Manage what each person
          or group may do on its connected producer APIs, all in one place.
        </CardDescription>

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
        {users.length === 0 && groups.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {bundleAccessApplicable
              ? "No one yet. Add a person or a group to grant bundle access and assign producer scopes."
              : "No one yet. Add a person or a group to assign their producer scopes."}
          </p>
        ) : users.length === 0 ? null : (
          <BundlePermissionPeople
            users={users}
            manageableProducers={manageableProducers}
            showHeading={groups.length > 0}
            onEdit={openEditForUser}
            onRemove={setRemoveTarget}
          />
        )}
        {groups.length > 0 && (
          <>
            {users.length > 0 && <Separator className="my-4" />}
            <BundlePermissionGroups
              agentId={agent.id}
              bundleUuid={bundleUuid}
              groups={groups}
              manageableProducers={manageableProducers}
              onEdit={openEditForGroup}
            />
          </>
        )}
      </CardContent>

      {/* Add a person or a group — fresh modal. */}
      <BundlePermissionsAddUserModal
        open={addOpen}
        onOpenChange={setAddOpen}
        agentId={agent.id}
        bundleUuid={bundleUuid}
        bundleAccessApplicable={bundleAccessApplicable}
        producers={producers}
        excludeUserIds={excludeUserIds}
        excludeGroupIds={excludeGroupIds}
      />

      {/* Edit an existing person or group (producer-cluster click). */}
      <BundlePermissionsAddUserModal
        open={!!editTarget}
        onOpenChange={(o) => {
          if (!o) setEditTarget(null)
        }}
        agentId={agent.id}
        bundleUuid={bundleUuid}
        bundleAccessApplicable={bundleAccessApplicable}
        producers={producers}
        excludeUserIds={excludeUserIds}
        fixedTarget={editTarget}
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
              {removeTarget?.full_name || removeTarget?.email || "this user"}?
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
