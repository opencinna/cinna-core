import {
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  type RowData,
  useReactTable,
} from "@tanstack/react-table"
import { type MouseEvent, useEffect } from "react"

import {
  DataTablePagination,
  DEFAULT_PAGE_SIZE,
} from "@/components/Common/DataTablePagination"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"

declare module "@tanstack/react-table" {
  // biome-ignore lint/correctness/noUnusedVariables: the generics must repeat the library's declaration for the merge to apply.
  interface ColumnMeta<TData extends RowData, TValue> {
    /**
     * Classes for both the column's header and its cells: a width
     * (`w-[170px]`), a responsive visibility (`hidden lg:table-cell`) — which
     * has to land on the `<th>` and every `<td>` alike, or the header row and
     * the body disagree about how many columns there are.
     */
    className?: string
  }
}

/**
 * Server-side paging, when the list is too long to hand over whole.
 *
 * Its absence is the default and keeps the client-side behaviour every existing
 * caller relies on: `data` is the entire list, the table slices it. When it is
 * present `data` is **one page**, the table stops slicing (`manualPagination`),
 * and the footer counts against `total` rather than `data.length` — otherwise a
 * 25-row page of a 500-key list reads "25 entries", which is the number the
 * pager exists to contradict.
 */
export interface DataTableServerPagination {
  pageIndex: number
  pageSize: number
  /** Total matching rows on the server, not the length of this page. */
  total: number
  onPageChange: (pageIndex: number) => void
  /**
   * Omit to hide "Rows per page". A size change is followed by
   * `onPageChange(0)`: the list starts over at its first page.
   */
  onPageSizeChange?: (pageSize: number) => void
}

interface DataTableProps<TData, TValue> {
  columns: ColumnDef<TData, TValue>[]
  data: TData[]
  /** Omit for client-side paging over the whole list. */
  serverPagination?: DataTableServerPagination
  /** Shown in place of "No results found." when the list is empty. */
  emptyState?: React.ReactNode
  /**
   * Stable identity per row. Defaults to the row's index, which is wrong for a
   * server-paged list: index 0 is a different entity on every page, so React
   * reuses the previous page's cells and any per-row state with them.
   */
  getRowId?: (row: TData) => string
  /**
   * Makes the whole row open the entity (§2 "Row as the control", table form).
   * An enhancement only: the row must also carry a real link (usually on the
   * name), which is what keyboard, middle-click and screen readers reach — so
   * the `<tr>` gets no `role="button"`. Clicks on a link or button inside the
   * row, and clicks that bubble in from a portaled menu or dialog, are ignored
   * here; anything else that should not navigate (a pending write on the row)
   * is the caller's check.
   */
  onRowClick?: (row: TData) => void
  /** Classes for the `<table>`, e.g. `table-fixed` for truncating cells. */
  tableClassName?: string
  /** The footer's noun: "12 credentials". Defaults to "entries". */
  itemLabel?: string
  /**
   * Client-side paging only. By default the table returns to page 1 whenever
   * `data` changes; pass `false` to keep the page across a refetch (a delete
   * on page 3). The page is still clamped when the list shrinks under it, and
   * a caller that wants a reset on a filter change remounts the table (`key`).
   */
  autoResetPageIndex?: boolean
  /**
   * Hide the header row visually, for a table whose rows read on their own (a
   * name, a few badges, a menu). It stays in the DOM as `sr-only`, so screen
   * readers still announce each cell's column.
   */
  hideHeader?: boolean
}

/**
 * Whether a click on a row is a click on the row itself. React bubbles events
 * through portals, so a menu item or dialog button rendered from inside a cell
 * still reaches the `<tr>`'s handler although it is nowhere near it in the DOM.
 */
function isRowOwnClick(event: MouseEvent<HTMLTableRowElement>): boolean {
  const target = event.target
  if (!(target instanceof Element)) return false
  if (!event.currentTarget.contains(target)) return false
  // The end of a drag-select is a click too; selecting a name is not opening it.
  if (window.getSelection()?.toString()) return false
  return target.closest("a, button, input, [role=menuitem]") === null
}

export function DataTable<TData, TValue>({
  columns,
  data,
  serverPagination,
  emptyState,
  getRowId,
  onRowClick,
  tableClassName,
  itemLabel,
  autoResetPageIndex,
  hideHeader = false,
}: DataTableProps<TData, TValue>) {
  const manual = serverPagination !== undefined
  const table = useReactTable({
    data,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getRowId: getRowId ? (row) => getRowId(row) : undefined,
    // Both models are still installed in the manual case: the row model is what
    // renders, and `getPaginationRowModel` is a no-op once `manualPagination`
    // tells the table the slicing has already happened.
    getPaginationRowModel: getPaginationRowModel(),
    // The server-paging options are spread in only when they apply. The table
    // merges options over its defaults with a plain spread, so an explicit
    // `onPaginationChange: undefined` replaces the built-in state updater and
    // every pager button in the client-side case silently does nothing.
    ...(manual
      ? {
          manualPagination: true,
          pageCount: Math.max(
            1,
            Math.ceil(serverPagination.total / serverPagination.pageSize),
          ),
          state: {
            pagination: {
              pageIndex: serverPagination.pageIndex,
              pageSize: serverPagination.pageSize,
            },
          },
          onPaginationChange: (updater) => {
            const current = {
              pageIndex: serverPagination.pageIndex,
              pageSize: serverPagination.pageSize,
            }
            const next =
              typeof updater === "function" ? updater(current) : updater
            // A new size starts over at the first page. The table would keep
            // the top row in view instead, but the index it derives for that
            // is reported separately from the size, and a caller that applies
            // them one after the other lands somewhere depending on the order.
            if (next.pageSize !== current.pageSize) {
              serverPagination.onPageSizeChange?.(next.pageSize)
              serverPagination.onPageChange(0)
              return
            }
            if (next.pageIndex !== current.pageIndex) {
              serverPagination.onPageChange(next.pageIndex)
            }
          },
        }
      : {
          // Spread in only when given: an explicit `undefined` would replace
          // the library's default, as with `onPaginationChange` above.
          ...(autoResetPageIndex !== undefined ? { autoResetPageIndex } : {}),
          initialState: {
            pagination: { pageIndex: 0, pageSize: DEFAULT_PAGE_SIZE },
          },
        }),
  })
  const total = manual ? serverPagination.total : data.length

  // A server-paged list can shrink under the current page (its last row
  // deleted). Step back to the last page that still exists — the footer may
  // already be gone, which would leave "No results found." with no way back.
  const pageCount = table.getPageCount()
  const serverPageIndex = serverPagination?.pageIndex
  const onServerPageChange = serverPagination?.onPageChange
  useEffect(() => {
    if (
      serverPageIndex !== undefined &&
      serverPageIndex > 0 &&
      serverPageIndex >= pageCount
    ) {
      onServerPageChange?.(pageCount - 1)
    }
  }, [serverPageIndex, pageCount, onServerPageChange])

  // The client-side counterpart, needed once auto-reset is off: a list that
  // shrinks under the current page steps back to its last page.
  const clientPageIndex = manual
    ? undefined
    : table.getState().pagination.pageIndex
  useEffect(() => {
    if (
      clientPageIndex !== undefined &&
      clientPageIndex > 0 &&
      clientPageIndex >= pageCount
    ) {
      table.setPageIndex(Math.max(0, pageCount - 1))
    }
  }, [clientPageIndex, pageCount, table])

  return (
    <div className="flex flex-col gap-4">
      {/* The table scrolls inside its own container rather than pushing the
          page sideways: a column set that fits at 1440 can still overflow at
          1024, and a horizontally scrolling page moves the sidebar with it. */}
      <div className="overflow-x-auto">
        <Table className={tableClassName}>
          {/* A hidden header still sizes the columns: `sr-only` on the
              `<thead>` would take it out of the table's layout, and a
              `table-fixed` table would then split its width evenly. So the
              row collapses to zero height and only its labels are `sr-only`. */}
          <TableHeader
            className={cn(hideHeader && "bg-transparent [&_tr]:border-b-0")}
          >
            {table.getHeaderGroups().map((headerGroup) => (
              <TableRow key={headerGroup.id} className="hover:bg-transparent">
                {headerGroup.headers.map((header) => {
                  return (
                    <TableHead
                      key={header.id}
                      className={cn(
                        header.column.columnDef.meta?.className,
                        hideHeader && "h-0 py-0",
                      )}
                    >
                      {header.isPlaceholder ? null : hideHeader ? (
                        <span className="sr-only">
                          {flexRender(
                            header.column.columnDef.header,
                            header.getContext(),
                          )}
                        </span>
                      ) : (
                        flexRender(
                          header.column.columnDef.header,
                          header.getContext(),
                        )
                      )}
                    </TableHead>
                  )
                })}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {table.getRowModel().rows.length ? (
              table.getRowModel().rows.map((row) => (
                <TableRow
                  key={row.id}
                  className={cn(onRowClick && "cursor-pointer")}
                  onClick={
                    onRowClick
                      ? (event) => {
                          if (isRowOwnClick(event)) onRowClick(row.original)
                        }
                      : undefined
                  }
                >
                  {row.getVisibleCells().map((cell) => (
                    <TableCell
                      key={cell.id}
                      className={cell.column.columnDef.meta?.className}
                    >
                      {flexRender(
                        cell.column.columnDef.cell,
                        cell.getContext(),
                      )}
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : (
              <TableRow className="hover:bg-transparent">
                <TableCell
                  colSpan={columns.length}
                  className="h-32 text-center text-muted-foreground"
                >
                  {emptyState ?? "No results found."}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      <DataTablePagination
        table={table}
        total={total}
        itemLabel={itemLabel}
        showPageSize={
          !manual || serverPagination.onPageSizeChange !== undefined
        }
      />
    </div>
  )
}
