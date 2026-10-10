import {
  useMutation,
  useMutationState,
  useQueryClient,
} from "@tanstack/react-query"
import { type ReactNode, useCallback, useMemo, useState } from "react"

import { ApiError, CatalogService } from "@/client"
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
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import type { CatalogItemRef } from "@/utils/catalogItems"
import { invalidateCatalogItem } from "./catalogQueries"
import { DeleteCatalogItemDialog } from "./DeleteCatalogItemDialog"
import { CatalogPeopleDialog } from "./People/CatalogPeopleDialog"

const DELETE_KEY = ["catalog-item", "delete"] as const
const LISTING_KEY = ["catalog-item", "listing"] as const

type ListingVars = { item: CatalogItemRef; listed: boolean }

interface Options {
  /** After a delete succeeds — a detail route navigates away, a table stays. */
  onDeleted?: (item: CatalogItemRef) => void
}

/**
 * The maintainer's writes on catalog items (S3), with the dialogs that confirm
 * them. A menu item only sets a target here: a `DropdownMenuItem` unmounts on
 * select and would take a nested confirm's pending state with it, so the
 * confirms are owned by whoever renders `dialogs` — a card, or a table for all
 * of its rows.
 */
export function useCatalogItemActions({ onDeleted }: Options = {}) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [peopleTarget, setPeopleTarget] = useState<CatalogItemRef | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<CatalogItemRef | null>(null)
  const [delistTarget, setDelistTarget] = useState<CatalogItemRef | null>(null)
  const [refusal, setRefusal] = useState<string | null>(null)

  const closeDelete = () => {
    setDeleteTarget(null)
    setRefusal(null)
  }

  const deleteMutation = useMutation({
    mutationKey: DELETE_KEY,
    mutationFn: (item: CatalogItemRef) =>
      CatalogService.deleteCatalogItem({ itemUuid: item.uuid }),
    onSuccess: (_data, item) => {
      showSuccessToast(`${item.display_name} deleted`)
      closeDelete()
      onDeleted?.(item)
    },
    onError: (err, item) => {
      if (err instanceof ApiError && err.status === 409) {
        setRefusal(
          getErrorMessage(
            err,
            "People still use this, so it can't be deleted.",
          ),
        )
        return
      }
      if (err instanceof ApiError && err.status === 404) {
        showErrorToast(`${item.display_name} was already deleted`)
        closeDelete()
        return
      }
      showErrorToast(getErrorMessage(err, "Could not delete this item"))
    },
    onSettled: (_data, _err, item) =>
      invalidateCatalogItem(queryClient, item.uuid),
  })

  const listingMutation = useMutation({
    mutationKey: LISTING_KEY,
    mutationFn: ({ item, listed }: ListingVars) =>
      CatalogService.updateCatalogItem({
        itemUuid: item.uuid,
        requestBody: { is_listed: listed },
      }),
    onSuccess: (_data, { item, listed }) => {
      showSuccessToast(
        listed
          ? `${item.display_name} is listed in the catalog`
          : `${item.display_name} delisted`,
      )
      setDelistTarget(null)
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Could not change the listing")),
    onSettled: (_data, _err, { item }) =>
      invalidateCatalogItem(queryClient, item.uuid),
  })

  // Every write in flight, not just this observer's latest call: one
  // `useMutation` describes only its last `mutate`, so two rows written in
  // quick succession would otherwise lose the first one's spinner.
  const pendingDeletes = useMutationState({
    filters: { mutationKey: DELETE_KEY, status: "pending" },
    select: (m) => (m.state.variables as CatalogItemRef | undefined)?.uuid,
  })
  const pendingListings = useMutationState({
    filters: { mutationKey: LISTING_KEY, status: "pending" },
    select: (m) => m.state.variables as ListingVars | undefined,
  })
  const pending = useMemo(() => {
    const byUuid = new Map<string, string>()
    for (const v of pendingListings) {
      if (v) byUuid.set(v.item.uuid, v.listed ? "Listing…" : "Delisting…")
    }
    for (const uuid of pendingDeletes) {
      if (uuid) byUuid.set(uuid, "Deleting…")
    }
    return byUuid
  }, [pendingDeletes, pendingListings])

  /** The wait in flight on one item, named for a row's spinner, or `null`. */
  const pendingLabel = useCallback(
    (uuid: string): string | null => pending.get(uuid) ?? null,
    [pending],
  )

  const deletePending = deleteTarget
    ? pendingDeletes.includes(deleteTarget.uuid)
    : false
  const delistPending = delistTarget
    ? pendingListings.some((v) => v?.item.uuid === delistTarget.uuid)
    : false

  // Stable, so a table can memoise the columns built from them.
  const openDelete = useCallback((item: CatalogItemRef) => {
    setRefusal(null)
    setDeleteTarget(item)
  }, [])
  const { mutate: mutateListing } = listingMutation
  /** Delisting confirms; listing again is reversible and does not. */
  const setListed = useCallback(
    (item: CatalogItemRef, listed: boolean) => {
      if (listed) mutateListing({ item, listed: true })
      else setDelistTarget(item)
    },
    [mutateListing],
  )

  const dialogs: ReactNode = (
    <>
      <CatalogPeopleDialog
        item={peopleTarget}
        onClose={() => setPeopleTarget(null)}
      />
      <DeleteCatalogItemDialog
        item={deleteTarget}
        isPending={deletePending}
        refusal={refusal}
        onConfirm={() => {
          if (deleteTarget) deleteMutation.mutate(deleteTarget)
        }}
        onClose={closeDelete}
      />
      <AlertDialog
        open={delistTarget !== null}
        onOpenChange={(next) => {
          if (!next && !delistPending) setDelistTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="break-words">
              Delist {delistTarget?.display_name}?
            </AlertDialogTitle>
            <AlertDialogDescription>
              It disappears from the catalog for everyone. It is not deleted,
              and copies people already have keep working.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={delistPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                if (delistTarget)
                  listingMutation.mutate({ item: delistTarget, listed: false })
              }}
              disabled={delistPending}
            >
              {delistPending ? "Delisting…" : "Delist"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )

  return {
    openPeople: setPeopleTarget,
    openDelete,
    setListed,
    pendingLabel,
    dialogs,
  }
}
