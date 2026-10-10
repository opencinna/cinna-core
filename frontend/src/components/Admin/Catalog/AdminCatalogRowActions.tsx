import { ArrowRight, Eye, EyeOff, Trash2, UserCog } from "lucide-react"

import type { CatalogItem } from "@/client"
import { PendingSlot } from "@/components/Catalog/PendingSlot"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"

export interface AdminCatalogRowHandlers {
  onOpen: (item: CatalogItem) => void
  onPeople: (item: CatalogItem) => void
  onSetListed: (item: CatalogItem, listed: boolean) => void
  onDelete: (item: CatalogItem) => void
  /** The wait in flight on an item, named ("Deleting…"), or `null`. */
  pendingLabel: (uuid: string) => string | null
}

/**
 * The `⋯` of one admin catalog row, or — while a write on it is in flight — a
 * named spinner in the same slot (§2 Pending on rows). Every item only sets
 * state the table owns: a `DropdownMenuItem` unmounts on select.
 */
export function AdminCatalogRowActions({
  item,
  onOpen,
  onPeople,
  onSetListed,
  onDelete,
  pendingLabel,
}: AdminCatalogRowHandlers & { item: CatalogItem }) {
  const pending = pendingLabel(item.uuid)
  if (pending) return <PendingSlot label={pending} />

  return (
    <RowActionsMenu label={item.display_name}>
      <DropdownMenuItem onSelect={() => onOpen(item)}>
        <ArrowRight />
        Open
      </DropdownMenuItem>
      <DropdownMenuItem onSelect={() => onPeople(item)}>
        <UserCog />
        People…
      </DropdownMenuItem>
      {item.is_listed ? (
        <DropdownMenuItem onSelect={() => onSetListed(item, false)}>
          <EyeOff />
          Delist from the catalog
        </DropdownMenuItem>
      ) : (
        <DropdownMenuItem onSelect={() => onSetListed(item, true)}>
          <Eye />
          List in the catalog
        </DropdownMenuItem>
      )}
      <DropdownMenuSeparator />
      <DropdownMenuItem variant="destructive" onSelect={() => onDelete(item)}>
        <Trash2 />
        Delete…
      </DropdownMenuItem>
    </RowActionsMenu>
  )
}
