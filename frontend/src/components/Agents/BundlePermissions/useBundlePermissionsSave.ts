import { useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { invalidateUserGroupShareLists } from "@/components/Admin/UserGroups/userGroupQueries"
import { invalidateBundleGroupGrants } from "@/components/Agents/BundlePermissions/BundlePermissionGroups"
import {
  extractError,
  type PlannedAction,
} from "@/components/Agents/BundlePermissions/bundlePermissionsActions"
import { agentApiAccessQueryKey } from "@/hooks/useAgentApiKeys"
import useCustomToast from "@/hooks/useCustomToast"

interface UseBundlePermissionsSaveParams {
  agentId: string
  bundleUuid: string | null
  onOpenChange: (open: boolean) => void
}

/**
 * Submit state and execution for the add/edit permissions modal.
 *
 * A submit (person or group alike) runs the planned actions sequentially,
 * accumulating a per-section error. Successful sections persist even when a
 * later one fails (no cross-domain transaction); the overview re-query is the
 * source of truth.
 */
export function useBundlePermissionsSave({
  agentId,
  bundleUuid,
  onOpenChange,
}: UseBundlePermissionsSaveParams) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [sectionErrors, setSectionErrors] = useState<Record<string, string>>({})
  const [submitting, setSubmitting] = useState(false)

  const resetSaveState = () => {
    setSectionErrors({})
    setSubmitting(false)
  }

  /**
   * `successMessage` is the toast when every section saved; `forGroup` also
   * refreshes the admin User Groups lists, which show a group's shares.
   */
  const submitActions = async (
    actions: PlannedAction[],
    successMessage: string,
    forGroup = false,
  ) => {
    if (actions.length === 0) return

    setSubmitting(true)
    setSectionErrors({})

    const errors: Record<string, string> = {}
    let bundleTouched = false
    const touchedProducerIds = new Set<string>()

    for (const action of actions) {
      try {
        await action.run()
        if (action.key === "bundle") bundleTouched = true
        if (action.producerAgentId)
          touchedProducerIds.add(action.producerAgentId)
      } catch (e) {
        errors[action.key] = extractError(e)
      }
    }

    // The overview re-query is the source of truth after any persisted change.
    queryClient.invalidateQueries({
      queryKey: ["bundlePermissionsOverview", agentId],
    })
    if (bundleTouched && bundleUuid) {
      queryClient.invalidateQueries({
        queryKey: ["bundles", bundleUuid, "grants"],
      })
      // A group's bundle grant is also a catalog grant on the same item.
      invalidateBundleGroupGrants(queryClient, agentId, bundleUuid)
    }
    for (const producerId of touchedProducerIds) {
      // The producer's API access card reads grants through its own list.
      queryClient.invalidateQueries({
        queryKey: agentApiAccessQueryKey(producerId),
      })
    }

    if (forGroup) invalidateUserGroupShareLists(queryClient)

    setSubmitting(false)

    const failedCount = Object.keys(errors).length
    if (failedCount === 0) {
      showSuccessToast(successMessage)
      onOpenChange(false)
    } else {
      setSectionErrors(errors)
      showErrorToast(
        `Saved with ${failedCount} issue${failedCount === 1 ? "" : "s"} — see details`,
      )
    }
  }

  return {
    submitting,
    sectionErrors,
    resetSaveState,
    submitActions,
  }
}
