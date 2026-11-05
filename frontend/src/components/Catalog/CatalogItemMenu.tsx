import { useNavigate } from "@tanstack/react-router"
import { Eye, EyeOff, Trash2, UserCog } from "lucide-react"
import type { ReactNode } from "react"

import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import useAuth from "@/hooks/useAuth"
import type { CatalogItemRef } from "@/utils/catalogItems"
import { PendingSlot } from "./PendingSlot"
import { useCatalogItemActions } from "./useCatalogItemActions"

interface CatalogItemMenuProps {
  item: CatalogItemRef
  /** The caller holds `maintain` on the item (P2); superusers always manage. */
  canMaintain: boolean
  /** Host-specific items between People… and the listing item (Edit details…). */
  extraItems?: ReactNode
  /**
   * Items for anyone who can see the item (Copy install link), first in the
   * menu. With these, a reader who may not manage still gets a menu holding
   * only them.
   */
  readerItems?: ReactNode
}

/**
 * S3c — the maintainer's `⋯` on a detail card's title row: People…, the host's
 * own items, the superuser's listing switch, then Delete…. Renders nothing for
 * someone who may not manage the item, unless the host passes `readerItems`,
 * which every reader gets. The confirms live here, beside the
 * menu, not inside its items. Deleting from a detail page lands on `/catalog`.
 */
export function CatalogItemMenu({
  item,
  canMaintain,
  extraItems,
  readerItems,
}: CatalogItemMenuProps) {
  const { user } = useAuth()
  const navigate = useNavigate()
  const isSuperuser = !!user?.is_superuser
  const actions = useCatalogItemActions({
    onDeleted: () => navigate({ to: "/catalog" }),
  })

  if (!canMaintain && !isSuperuser) {
    return readerItems ? (
      <RowActionsMenu label={item.display_name}>{readerItems}</RowActionsMenu>
    ) : null
  }
  const pending = actions.pendingLabel(item.uuid)

  return (
    <>
      {pending ? (
        <PendingSlot label={pending} />
      ) : (
        <RowActionsMenu label={item.display_name}>
          {readerItems}
          {readerItems && <DropdownMenuSeparator />}
          <DropdownMenuItem onSelect={() => actions.openPeople(item)}>
            <UserCog />
            People…
          </DropdownMenuItem>
          {extraItems}
          {isSuperuser &&
            (item.is_listed ? (
              <DropdownMenuItem onSelect={() => actions.setListed(item, false)}>
                <EyeOff />
                Delist from the catalog
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem onSelect={() => actions.setListed(item, true)}>
                <Eye />
                List in the catalog
              </DropdownMenuItem>
            ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem
            variant="destructive"
            onSelect={() => actions.openDelete(item)}
          >
            <Trash2 />
            Delete…
          </DropdownMenuItem>
        </RowActionsMenu>
      )}
      {actions.dialogs}
    </>
  )
}
