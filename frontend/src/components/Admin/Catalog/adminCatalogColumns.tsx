import type { ColumnDef } from "@tanstack/react-table"

import type { CatalogItem } from "@/client"
import { RelativeTime } from "@/components/Common/RelativeTime"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import {
  CATALOG_KINDS,
  catalogAccessFact,
  catalogKindOf,
  catalogVersionLabel,
  peopleCount,
} from "@/utils/catalogItems"
import {
  AdminCatalogRowActions,
  type AdminCatalogRowHandlers,
} from "./AdminCatalogRowActions"

const VISIBLE_MAINTAINERS = 2

/** "Maintained by A, B +N" — the Name cell's second line. */
function MaintainersLine({ item }: { item: CatalogItem }) {
  const names = (item.maintainers ?? []).map(
    (m) => m.display_name.trim() || "Unknown person",
  )
  if (names.length === 0) return null
  const shown = names.slice(0, VISIBLE_MAINTAINERS)
  const rest = names.length - shown.length
  // The tooltip lists every maintainer, not only the `+N`: at 1024 the shown
  // names are cut short too, and this is the one place they can be read.
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={`Maintainers: ${names.join(", ")}`}
          className="flex max-w-full cursor-default items-center text-left text-xs text-muted-foreground"
        >
          <span className="truncate">Maintained by {shown.join(", ")}</span>
          {rest > 0 && (
            <span className="ml-1 shrink-0 text-muted-foreground">+{rest}</span>
          )}
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        {names.map((name, i) => (
          <div key={`${name}-${i}`}>{name}</div>
        ))}
      </TooltipContent>
    </Tooltip>
  )
}

export function adminCatalogColumns(
  handlers: AdminCatalogRowHandlers,
): ColumnDef<CatalogItem>[] {
  return [
    {
      accessorKey: "display_name",
      header: "Name",
      cell: ({ row }) => {
        const item = row.original
        const kind = CATALOG_KINDS[catalogKindOf(item)]
        const Icon = kind.icon
        return (
          <div
            className={cn(
              "flex min-w-0 max-w-[200px] items-start gap-2 xl:max-w-[360px]",
              !item.is_listed && "opacity-60",
            )}
          >
            {/* The kind is this glyph (a flag, as on every list row), with its
                word in the tooltip and for screen readers — a Runs column
                beside it did not fit at 1024. */}
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="flex shrink-0 items-center pt-1">
                  <Icon className="h-3.5 w-3.5 text-muted-foreground" />
                  <span className="sr-only">{kind.word}</span>
                </span>
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                {kind.word}
              </TooltipContent>
            </Tooltip>
            {/* Maintainers ride under the name: a column of their own did
                not fit at 1024 without capping both to a few characters. */}
            <div className="min-w-0">
              <span className="block truncate font-medium">
                {item.display_name}
              </span>
              <MaintainersLine item={item} />
            </div>
          </div>
        )
      },
    },
    {
      id: "audience",
      header: "Who can get it",
      cell: ({ row }) => {
        const item = row.original
        return (
          <div className="text-sm">
            <span className="block whitespace-normal">
              {catalogAccessFact({ ...item, is_listed: true })}
            </span>
            {!item.is_listed && (
              <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <span className="h-2 w-2 shrink-0 rounded-full bg-warning" />
                Delisted
              </span>
            )}
          </div>
        )
      },
    },
    {
      // Version and when it was shared, as one cell: two facts about the same
      // revision, and a seventh column did not fit at 1024.
      id: "version",
      header: "Version",
      cell: ({ row }) => {
        const latest = row.original.latest
        if (!latest) return null
        return (
          <div className="text-sm">
            <span className="block whitespace-nowrap">
              {catalogVersionLabel(latest)}
            </span>
            <RelativeTime
              timestamp={latest.published_at}
              showTooltip
              className="block text-xs whitespace-nowrap text-muted-foreground"
            />
          </div>
        )
      },
    },
    {
      id: "installs",
      header: "Installs",
      cell: ({ row }) => (
        <span className="whitespace-nowrap text-sm">
          {peopleCount(row.original.install_count ?? 0)}
        </span>
      ),
    },
    {
      id: "actions",
      header: () => <span className="sr-only">Actions</span>,
      cell: ({ row }) => (
        <div className="flex justify-end">
          <AdminCatalogRowActions item={row.original} {...handlers} />
        </div>
      ),
    },
  ]
}
