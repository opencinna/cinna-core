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
}

/**
 * S3c — the maintainer's `⋯` on a detail card's title row: People…, the host's
 * own items, the superuser's listing switch, then Delete…. Renders nothing for
 * someone who may not manage the item. The confirms live here, beside the
 * menu, not inside its items. Deleting from a detail page lands on `/catalog`.
 */
export function CatalogItemMenu({
  item,
  canMaintain,
  extraItems,
}: CatalogItemMenuProps) {
  const { user } = useAuth()
  const navigate = useNavigate()
  const isSuperuser = !!user?.is_superuser
  const actions = useCatalogItemActions({
    onDeleted: () => navigate({ to: "/catalog" }),
  })

  if (!canMaintain && !isSuperuser) return null
  const pending = actions.pendingLabel(item.uuid)

  return (
    <>
      {pending ? (
        <PendingSlot label={pending} />
      ) : (
        <RowActionsMenu label={item.display_name}>
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
