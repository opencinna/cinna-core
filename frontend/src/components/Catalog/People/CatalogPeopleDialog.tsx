import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { type ReactNode, useState } from "react"

import { ApiError, CatalogService } from "@/client"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import {
  type CatalogItemRef,
  catalogKindOf,
  catalogNoun,
  isCatalogVisibility,
} from "@/utils/catalogItems"
import { invalidateCatalogItem } from "../catalogQueries"
import { CatalogPeopleForm, type CatalogPeopleSave } from "./CatalogPeopleForm"

interface CatalogPeopleDialogProps {
  /** The item whose access is edited; `null` closes the dialog. */
  item: CatalogItemRef | null
  onClose: () => void
}

/**
 * S3a — who can find one catalog item, what each person, group or role may
 * do with it, and who looks after it. Titled by the item; the body is a
 * buffered form whose Save replaces the whole grant list and, when it changed,
 * the general access. The `edit` level's words follow the item's kind.
 * Rebuilt from the team library's access dialog (d5bb0a16).
 */
export function CatalogPeopleDialog({
  item,
  onClose,
}: CatalogPeopleDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [refusal, setRefusal] = useState<string | null>(null)
  const uuid = item?.uuid ?? ""
  const visibility =
    item && isCatalogVisibility(item.visibility) ? item.visibility : "private"

  const grantsQuery = useQuery({
    queryKey: ["catalog-item-grants", uuid],
    queryFn: () => CatalogService.listCatalogItemGrants({ itemUuid: uuid }),
    enabled: item !== null,
    // The form seeds its buffer once from this data, so it must never be a
    // cached copy a background refetch later replaces: Save would then PUT the
    // stale list over newer grants.
    gcTime: 0,
    refetchOnWindowFocus: false,
  })

  const saveMutation = useMutation({
    mutationFn: async (next: CatalogPeopleSave) => {
      const putGrants = () =>
        CatalogService.putCatalogItemGrants({
          itemUuid: uuid,
          requestBody: next.grants,
        })
      const patchVisibility = () =>
        CatalogService.updateCatalogItem({
          itemUuid: uuid,
          requestBody: { visibility: next.visibility },
        })
      if (next.visibility === visibility) {
        await putGrants()
      } else if (next.visibility === "users") {
        // Narrowing to named people: the audience exists before it is named.
        await patchVisibility()
        await putGrants()
      } else {
        await putGrants()
        await patchVisibility()
      }
    },
    onSuccess: () => {
      showSuccessToast(`Access to ${item?.display_name || "the item"} updated`)
      close()
    },
    onError: (err) => {
      // A refusal is a sentence about this list (no maintainer, Maintainer on
      // a role) — it belongs beside the list, with the edits kept.
      if (err instanceof ApiError) {
        if (err.status === 404) {
          showErrorToast("This item was already deleted")
          close()
          return
        }
        if (err.status === 409 || err.status === 422) {
          setRefusal(getErrorMessage(err, "Could not save access"))
          return
        }
      }
      showErrorToast(getErrorMessage(err, "Could not save access"))
    },
    onSettled: () => {
      invalidateCatalogItem(queryClient, uuid)
      // A bundle's group grants also render on its publisher's Bundle tab.
      queryClient.invalidateQueries({ queryKey: ["bundlePermissionsOverview"] })
    },
  })

  function close() {
    setRefusal(null)
    saveMutation.reset()
    onClose()
  }

  let body: ReactNode
  if (grantsQuery.isError) {
    body = (
      <>
        <QueryErrorAlert
          error={grantsQuery.error}
          fallback="Could not load access"
          onRetry={() => grantsQuery.refetch()}
        />
        <DialogFooter>
          <Button type="button" variant="outline" onClick={close}>
            Cancel
          </Button>
        </DialogFooter>
      </>
    )
  } else if (grantsQuery.data === undefined) {
    body = (
      <>
        <div className="space-y-1">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-[40px] w-full rounded-md" />
          ))}
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={close}>
            Cancel
          </Button>
          <Button type="button" disabled>
            Save
          </Button>
        </DialogFooter>
      </>
    )
  } else {
    body = (
      <CatalogPeopleForm
        // Re-seed the buffer when the dialog moves to another item.
        key={uuid}
        grants={grantsQuery.data}
        kind={item ? catalogKindOf(item) : "cloud"}
        visibility={visibility}
        isSaving={saveMutation.isPending}
        refusal={refusal}
        onEdited={() => setRefusal(null)}
        onSave={(next) => saveMutation.mutate(next)}
        onCancel={close}
      />
    )
  }

  return (
    <Dialog
      open={item !== null}
      onOpenChange={(next) => {
        if (!next && !saveMutation.isPending) close()
      }}
    >
      <DialogContent className="sm:max-w-lg max-h-[85vh] overflow-y-auto overflow-x-hidden [&>*]:min-w-0">
        <DialogHeader>
          <DialogTitle className="break-words">
            {item?.display_name}
          </DialogTitle>
          <DialogDescription className="break-words">
            Choose who can find this {item ? catalogNoun(item) : "item"} and who
            can change it.
          </DialogDescription>
        </DialogHeader>
        {body}
      </DialogContent>
    </Dialog>
  )
}
