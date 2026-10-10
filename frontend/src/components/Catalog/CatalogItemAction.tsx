import { ArrowUpCircle, Download, Laptop, Loader2 } from "lucide-react"
import type { MouseEvent } from "react"

import type { CatalogItem } from "@/client"
import { Button } from "@/components/ui/button"
import {
  catalogKindOf,
  cinnaCatalogLink,
  hasUpdate,
  myCloudInstall,
} from "@/utils/catalogItems"

/** The tile's writes, owned by the tile so its body can ignore clicks meanwhile. */
export interface CatalogItemWrites {
  installing: boolean
  updating: boolean
  install: () => void
  update: () => void
  open: () => void
  addToAgent: () => void
}

interface CatalogItemActionProps {
  item: CatalogItem
  writes: CatalogItemWrites
}

/**
 * The one footer action a catalog tile carries, chosen by kind and by the
 * caller's install state. Every handler stops propagation: the whole tile is a
 * navigation target and this button is the one region that means something
 * else.
 */
export function CatalogItemAction({ item, writes }: CatalogItemActionProps) {
  const stop =
    (fn: () => void) =>
    (e: MouseEvent<HTMLButtonElement | HTMLAnchorElement>) => {
      e.stopPropagation()
      fn()
    }

  switch (catalogKindOf(item)) {
    case "skill":
      return (
        <Button
          variant="outline"
          className="w-full"
          onClick={stop(writes.addToAgent)}
        >
          <Download className="mr-2 h-4 w-4" />
          Add to agent
        </Button>
      )
    case "desktop":
      // Same label whatever the install state: a label is not a state readout
      // (§6), and the tile's own line says how many computers have it.
      return (
        <Button variant="outline" className="w-full" asChild>
          <a href={cinnaCatalogLink(item)} onClick={(e) => e.stopPropagation()}>
            <Laptop className="mr-2 h-4 w-4" />
            Open in Cinna Desktop
          </a>
        </Button>
      )
    default:
      break
  }

  if (!myCloudInstall(item)) {
    return (
      <Button
        className="w-full"
        onClick={stop(writes.install)}
        disabled={writes.installing}
      >
        {writes.installing ? (
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        ) : (
          <Download className="mr-2 h-4 w-4" />
        )}
        {writes.installing ? "Installing…" : "Quick Install"}
      </Button>
    )
  }

  if (hasUpdate(item)) {
    return (
      <Button
        className="w-full"
        onClick={stop(writes.update)}
        disabled={writes.updating}
      >
        {writes.updating ? (
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        ) : (
          <ArrowUpCircle className="mr-2 h-4 w-4" />
        )}
        {writes.updating ? "Updating…" : "Update"}
      </Button>
    )
  }

  return (
    <Button variant="outline" className="w-full" onClick={stop(writes.open)}>
      Open
    </Button>
  )
}
