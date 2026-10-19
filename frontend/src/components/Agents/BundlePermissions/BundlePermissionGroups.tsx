import {
  type QueryClient,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query"
import { Pencil, Trash2 } from "lucide-react"
import { Fragment, useState } from "react"

import type {
  ApiError,
  BundlePermissionGroup,
  BundlePermissionProducer,
} from "@/client"
import { AgentApiService, InstallsService } from "@/client"
import { invalidateUserGroupShareLists } from "@/components/Admin/UserGroups/userGroupQueries"
import { existingGroupGrantFor } from "@/components/Agents/BundlePermissions/bundlePermissionsActions"
import { ProducerScopeInline } from "@/components/Agents/BundlePermissions/ProducerScopeInline"
import { invalidateCatalogItem } from "@/components/Catalog/catalogQueries"
import { ListRow, ListRowGroup } from "@/components/Common/ListRow"
import { PreviewList } from "@/components/Common/PreviewList"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
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
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { agentApiAccessQueryKey } from "@/hooks/useAgentApiKeys"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { pluralize } from "@/utils/pluralize"

/**
 * After any write to a bundle's group grants: the overview this card reads,
 * and the catalog reads of the same bundle (its People dialog's grant list
 * included — a bundle's catalog item uuid is its `bundle_uuid`).
 */
export function invalidateBundleGroupGrants(
  queryClient: QueryClient,
  agentId: string,
  bundleUuid: string,
) {
  queryClient.invalidateQueries({
    queryKey: ["bundlePermissionsOverview", agentId],
  })
  invalidateCatalogItem(queryClient, bundleUuid)
}

interface BundlePermissionGroupRowProps {
  group: BundlePermissionGroup
  manageableProducers: BundlePermissionProducer[]
  onEdit: (group: BundlePermissionGroup) => void
  onRemove: (group: BundlePermissionGroup) => void
}

/**
 * One group in the card — the person row's skeleton: no level (being listed
 * with a bundle grant means its members can use and install the bundle), its
 * member count as meta, one scope cluster per manageable producer, and the
 * same Edit / Remove menu.
 */
function BundlePermissionGroupRow({
  group,
  manageableProducers,
  onEdit,
  onRemove,
}: BundlePermissionGroupRowProps) {
  return (
    <ListRow
      title={group.name}
      meta={pluralize(group.member_count ?? 0, "member", "members")}
      flags={manageableProducers.map((producer) => (
        <ProducerScopeInline
          key={producer.producer_agent_id}
          producer={producer}
          grant={existingGroupGrantFor(producer, group.group_id)}
          onEdit={() => onEdit(group)}
        />
      ))}
    >
      <RowActionsMenu label={`${group.name}'s permissions`}>
        <DropdownMenuItem onSelect={() => onEdit(group)}>
          <Pencil />
          Edit permissions
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          variant="destructive"
          onSelect={(e) => {
            e.preventDefault()
            onRemove(group)
          }}
        >
          <Trash2 />
          Remove access
        </DropdownMenuItem>
      </RowActionsMenu>
    </ListRow>
  )
}

/** "its access to this bundle and its API scopes on 2 producers you manage". */
function removalSentence(
  group: BundlePermissionGroup,
  scopedProducerCount: number,
): string {
  const parts = [
    group.bundle_grant_id ? "its access to this bundle" : null,
    scopedProducerCount > 0
      ? `its API scopes on ${pluralize(scopedProducerCount, "producer", "producers")} you manage`
      : null,
  ].filter(Boolean)
  const what = parts.length > 0 ? parts.join(" and ") : "its access record"
  return `This removes ${what}. Members keep what they hold another way — individually or through another group. Producers managed by another owner are not affected.`
}

interface BundlePermissionGroupsProps {
  agentId: string
  bundleUuid: string
  /** Sorted by name (the server's order). Never empty — the card skips it. */
  groups: BundlePermissionGroup[]
  manageableProducers: BundlePermissionProducer[]
  /** Opens the card's edit modal for this group. */
  onEdit: (group: BundlePermissionGroup) => void
}

/**
 * The card's "Groups" section: first five rows, "Show all (N)" into a Sheet
 * of the same rows, and the cascading remove confirm both hosts share.
 */
export function BundlePermissionGroups({
  agentId,
  bundleUuid,
  groups,
  manageableProducers,
  onEdit,
}: BundlePermissionGroupsProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [showAll, setShowAll] = useState(false)
  const [removeTarget, setRemoveTarget] =
    useState<BundlePermissionGroup | null>(null)

  const scopedProducers = (group: BundlePermissionGroup) =>
    manageableProducers.flatMap((producer) => {
      const grant = existingGroupGrantFor(producer, group.group_id)
      return grant ? [{ producer, grant }] : []
    })

  /**
   * The group's whole record in one step, mirroring the person remove: the
   * bundle grant (if any) plus its scope grant on every manageable producer,
   * fanned out with `allSettled` so a partial failure still removes what it
   * can. The overview re-query is the source of truth afterwards.
   */
  const removeMutation = useMutation({
    mutationFn: async (group: BundlePermissionGroup) => {
      const tasks: {
        label: string
        producerId?: string
        run: () => Promise<unknown>
      }[] = []
      if (group.bundle_grant_id) {
        const grantId = group.bundle_grant_id
        tasks.push({
          label: "bundle access",
          run: () =>
            InstallsService.revokeBundleGroupGrant({ agentId, grantId }),
        })
      }
      for (const { producer, grant } of scopedProducers(group)) {
        tasks.push({
          label: producer.producer_agent_name || "producer scopes",
          producerId: producer.producer_agent_id,
          run: () =>
            AgentApiService.deleteAgentApiGroupGrant({
              agentId: producer.producer_agent_id,
              grantId: grant.grant_id,
            }),
        })
      }

      const results = await Promise.allSettled(tasks.map((t) => t.run()))
      const touchedProducerIds = new Set<string>()
      const failures: string[] = []
      results.forEach((result, i) => {
        const task = tasks[i]
        if (result.status === "rejected") failures.push(task.label)
        else if (task.producerId) touchedProducerIds.add(task.producerId)
      })
      return { failures, touchedProducerIds }
    },
    onSuccess: ({ failures, touchedProducerIds }, group) => {
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
        showSuccessToast(`${group.name} no longer has access`)
      }
      setRemoveTarget(null)
    },
    onError: (err) => {
      handleError.bind(showErrorToast)(err as ApiError)
      setRemoveTarget(null)
    },
    onSettled: () => {
      invalidateBundleGroupGrants(queryClient, agentId, bundleUuid)
      invalidateUserGroupShareLists(queryClient)
    },
  })

  const renderRow = (group: BundlePermissionGroup) => (
    <BundlePermissionGroupRow
      group={group}
      manageableProducers={manageableProducers}
      onEdit={onEdit}
      onRemove={setRemoveTarget}
    />
  )

  return (
    <section className="space-y-2">
      {/* Only a group's active members gain access; that is what the row's
          count says, so the section needs no description of its own. */}
      <h3 className="text-sm font-medium">Groups</h3>
      <PreviewList
        items={groups}
        getKey={(group) => group.group_id}
        renderItem={renderRow}
        // The card renders this section only once the overview is loaded and
        // holds a group, so the error and empty branches are never reached.
        errorFallback="Couldn't load groups."
        empty={null}
        onShowAll={() => setShowAll(true)}
      />

      <Sheet open={showAll} onOpenChange={setShowAll}>
        <SheetContent side="right" className="w-full sm:max-w-lg">
          <SheetHeader>
            <SheetTitle>Groups with access</SheetTitle>
            <SheetDescription>
              {pluralize(groups.length, "group", "groups")}
            </SheetDescription>
          </SheetHeader>
          <div className="flex-1 overflow-y-auto px-4 pb-4">
            <ListRowGroup>
              {groups.map((group) => (
                <Fragment key={group.group_id}>{renderRow(group)}</Fragment>
              ))}
            </ListRowGroup>
          </div>
        </SheetContent>
      </Sheet>

      <AlertDialog
        open={!!removeTarget}
        onOpenChange={(open) => {
          if (!open && !removeMutation.isPending) setRemoveTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="break-words">
              Remove {removeTarget?.name || "this group"}?
            </AlertDialogTitle>
            <AlertDialogDescription className="break-words">
              {removeTarget
                ? removalSentence(
                    removeTarget,
                    scopedProducers(removeTarget).length,
                  )
                : null}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={removeMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                if (removeTarget) removeMutation.mutate(removeTarget)
              }}
              disabled={removeMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {removeMutation.isPending ? "Removing..." : "Remove access"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  )
}
