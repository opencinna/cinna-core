import {
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  useReactTable,
} from "@tanstack/react-table"
import {
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

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
}

export function DataTable<TData, TValue>({
  columns,
  data,
  serverPagination,
  emptyState,
  getRowId,
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
    manualPagination: manual,
    pageCount: manual
      ? Math.max(
          1,
          Math.ceil(serverPagination.total / serverPagination.pageSize),
        )
      : undefined,
    state: manual
      ? {
          pagination: {
            pageIndex: serverPagination.pageIndex,
            pageSize: serverPagination.pageSize,
          },
        }
      : undefined,
    onPaginationChange: manual
      ? (updater) => {
          const next =
            typeof updater === "function"
              ? updater({
                  pageIndex: serverPagination.pageIndex,
                  pageSize: serverPagination.pageSize,
                })
              : updater
          if (next.pageSize !== serverPagination.pageSize) {
            serverPagination.onPageSizeChange?.(next.pageSize)
          }
          if (next.pageIndex !== serverPagination.pageIndex) {
            serverPagination.onPageChange(next.pageIndex)
          }
        }
      : undefined,
  })
  const total = manual ? serverPagination.total : data.length

  return (
    <div className="flex flex-col gap-4">
      {/* The table scrolls inside its own container rather than pushing the
          page sideways: a column set that fits at 1440 can still overflow at
          1024, and a horizontally scrolling page moves the sidebar with it. */}
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            {table.getHeaderGroups().map((headerGroup) => (
              <TableRow key={headerGroup.id} className="hover:bg-transparent">
                {headerGroup.headers.map((header) => {
                  return (
                    <TableHead key={header.id}>
                      {header.isPlaceholder
                        ? null
                        : flexRender(
                            header.column.columnDef.header,
                            header.getContext(),
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
                <TableRow key={row.id}>
                  {row.getVisibleCells().map((cell) => (
                    <TableCell key={cell.id}>
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

      {table.getPageCount() > 1 && (
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 p-4 border-t bg-muted/20">
          <div className="flex flex-col sm:flex-row sm:items-center gap-4">
            <div className="text-sm text-muted-foreground">
              Showing{" "}
              {table.getState().pagination.pageIndex *
                table.getState().pagination.pageSize +
                1}{" "}
              to{" "}
              {Math.min(
                (table.getState().pagination.pageIndex + 1) *
                  table.getState().pagination.pageSize,
                total,
              )}{" "}
              of <span className="font-medium text-foreground">{total}</span>{" "}
              entries
            </div>
            <div className="flex items-center gap-x-2">
              <p className="text-sm text-muted-foreground">Rows per page</p>
              <Select
                value={`${table.getState().pagination.pageSize}`}
                onValueChange={(value) => {
                  table.setPageSize(Number(value))
                }}
              >
                <SelectTrigger className="h-8 w-[70px]">
                  <SelectValue
                    placeholder={table.getState().pagination.pageSize}
                  />
                </SelectTrigger>
                <SelectContent side="top">
                  {[5, 10, 25, 50].map((pageSize) => (
                    <SelectItem key={pageSize} value={`${pageSize}`}>
                      {pageSize}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="flex items-center gap-x-6">
            <div className="flex items-center gap-x-1 text-sm text-muted-foreground">
              <span>Page</span>
              <span className="font-medium text-foreground">
                {table.getState().pagination.pageIndex + 1}
              </span>
              <span>of</span>
              <span className="font-medium text-foreground">
                {table.getPageCount()}
              </span>
            </div>

            <div className="flex items-center gap-x-1">
              <Button
                variant="outline"
                size="sm"
                className="h-8 w-8 p-0"
                onClick={() => table.setPageIndex(0)}
                disabled={!table.getCanPreviousPage()}
              >
                <span className="sr-only">Go to first page</span>
                <ChevronsLeft className="h-4 w-4" />
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="h-8 w-8 p-0"
                onClick={() => table.previousPage()}
                disabled={!table.getCanPreviousPage()}
              >
                <span className="sr-only">Go to previous page</span>
                <ChevronLeft className="h-4 w-4" />
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="h-8 w-8 p-0"
                onClick={() => table.nextPage()}
                disabled={!table.getCanNextPage()}
              >
                <span className="sr-only">Go to next page</span>
                <ChevronRight className="h-4 w-4" />
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="h-8 w-8 p-0"
                onClick={() => table.setPageIndex(table.getPageCount() - 1)}
                disabled={!table.getCanNextPage()}
              >
                <span className="sr-only">Go to last page</span>
                <ChevronsRight className="h-4 w-4" />
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
