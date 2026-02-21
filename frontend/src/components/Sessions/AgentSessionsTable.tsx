import { useState } from "react"
import { useNavigate } from "@tanstack/react-router"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  useReactTable,
  type RowSelectionState,
} from "@tanstack/react-table"
import {
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Trash2,
  MessageCircle,
  Wrench,
} from "lucide-react"

import type { SessionPublicExtended } from "@/client"
import { SessionsService } from "@/client"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Badge } from "@/components/ui/badge"
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
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import { RelativeTime } from "@/components/Common/RelativeTime"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

function getStatusBadge(session: SessionPublicExtended) {
  if (session.result_state === "completed" || session.status === "completed") {
    return (
      <Badge variant="outline" className="border-green-500 text-green-700 dark:text-green-400">
        Completed
      </Badge>
    )
  }
  if (session.result_state === "needs_input") {
    return (
      <Badge variant="outline" className="border-yellow-500 text-yellow-700 dark:text-yellow-400">
        Awaiting Feedback
      </Badge>
    )
  }
  if (session.result_state === "error" || session.status === "error") {
    return (
      <Badge variant="outline" className="border-red-500 text-red-700 dark:text-red-400">
        Error
      </Badge>
    )
  }
  if (session.interaction_status === "running") {
    return (
      <Badge variant="outline" className="border-blue-500 text-blue-700 dark:text-blue-400">
        Running
      </Badge>
    )
  }
  return (
    <Badge variant="outline" className="text-muted-foreground">
      Active
    </Badge>
  )
}

const columns: ColumnDef<SessionPublicExtended>[] = [
  {
    id: "select",
    header: ({ table }) => (
      <Checkbox
        checked={
          table.getIsAllPageRowsSelected() ||
          (table.getIsSomePageRowsSelected() && "indeterminate")
        }
        onCheckedChange={(value) => table.toggleAllPageRowsSelected(!!value)}
        aria-label="Select all"
      />
    ),
    cell: ({ row }) => (
      <Checkbox
        checked={row.getIsSelected()}
        onCheckedChange={(value) => row.toggleSelected(!!value)}
        aria-label="Select row"
      />
    ),
    enableSorting: false,
    enableHiding: false,
    size: 40,
  },
  {
    accessorKey: "title",
    header: "Title",
    cell: ({ row }) => {
      const title = row.original.title
      const isBuilding = row.original.mode === "building"
      return (
        <div className="flex items-center gap-2 min-w-0">
          {isBuilding ? (
            <Wrench className="h-3.5 w-3.5 text-orange-500 shrink-0" />
          ) : (
            <MessageCircle className="h-3.5 w-3.5 text-blue-500 shrink-0" />
          )}
          <span className="font-medium truncate max-w-xs">
            {title || "Untitled"}
          </span>
        </div>
      )
    },
  },
  {
    accessorKey: "last_message_content",
    header: "Last Message",
    cell: ({ row }) => {
      const content = row.original.last_message_content
      if (!content) {
        return <span className="text-muted-foreground italic">No messages</span>
      }
      return (
        <span className="text-muted-foreground truncate block max-w-sm text-sm">
          {content.length > 120 ? content.slice(0, 120) + "..." : content}
        </span>
      )
    },
  },
  {
    id: "status",
    header: "Status",
    cell: ({ row }) => getStatusBadge(row.original),
  },
  {
    accessorKey: "message_count",
    header: "Messages",
    cell: ({ row }) => (
      <span className="text-muted-foreground tabular-nums">
        {row.original.message_count ?? 0}
      </span>
    ),
    size: 90,
  },
  {
    id: "date",
    header: "Last Activity",
    cell: ({ row }) => (
      <span className="text-muted-foreground text-sm whitespace-nowrap">
        <RelativeTime timestamp={row.original.last_message_at || row.original.created_at} />
      </span>
    ),
  },
]

interface AgentSessionsTableProps {
  sessions: SessionPublicExtended[]
}

export function AgentSessionsTable({ sessions }: AgentSessionsTableProps) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({})
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)

  const table = useReactTable({
    data: sessions,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    onRowSelectionChange: setRowSelection,
    state: {
      rowSelection,
    },
    getRowId: (row) => row.id,
    initialState: {
      pagination: {
        pageSize: 10,
      },
    },
  })

  const selectedSessionIds = Object.keys(rowSelection)
  const selectedCount = selectedSessionIds.length

  const bulkDeleteMutation = useMutation({
    mutationFn: (ids: string[]) =>
      SessionsService.bulkDeleteSessions({
        requestBody: { session_ids: ids },
      }),
    onSuccess: (data) => {
      showSuccessToast(`${data.deleted_count} session(s) deleted successfully`)
      setRowSelection({})
      setDeleteDialogOpen(false)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries()
    },
  })

  const handleRowClick = (sessionId: string) => {
    navigate({
      to: "/session/$sessionId",
      params: { sessionId },
      search: { initialMessage: undefined, fileIds: undefined, fileObjects: undefined },
    })
  }

  return (
    <div className="flex flex-col gap-4">
      {/* Bulk Actions Bar */}
      {selectedCount > 0 && (
        <div className="flex items-center gap-3 p-3 bg-muted/50 rounded-lg border">
          <span className="text-sm text-muted-foreground">
            {selectedCount} session{selectedCount !== 1 ? "s" : ""} selected
          </span>
          <Button
            variant="destructive"
            size="sm"
            onClick={() => setDeleteDialogOpen(true)}
          >
            <Trash2 className="h-4 w-4 mr-1" />
            Delete Selected
          </Button>
        </div>
      )}

      <Table>
        <TableHeader>
          {table.getHeaderGroups().map((headerGroup) => (
            <TableRow key={headerGroup.id} className="hover:bg-transparent">
              {headerGroup.headers.map((header) => (
                <TableHead key={header.id} style={{ width: header.column.getSize() !== 150 ? header.column.getSize() : undefined }}>
                  {header.isPlaceholder
                    ? null
                    : flexRender(
                        header.column.columnDef.header,
                        header.getContext(),
                      )}
                </TableHead>
              ))}
            </TableRow>
          ))}
        </TableHeader>
        <TableBody>
          {table.getRowModel().rows.length ? (
            table.getRowModel().rows.map((row) => (
              <TableRow
                key={row.id}
                data-state={row.getIsSelected() && "selected"}
                className="cursor-pointer"
                onClick={(e) => {
                  // Don't navigate when clicking checkbox
                  if ((e.target as HTMLElement).closest('[role="checkbox"]')) return
                  handleRowClick(row.original.id)
                }}
              >
                {row.getVisibleCells().map((cell) => (
                  <TableCell key={cell.id}>
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
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
                No sessions found.
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>

      {/* Pagination */}
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
                sessions.length,
              )}{" "}
              of{" "}
              <span className="font-medium text-foreground">{sessions.length}</span>{" "}
              sessions
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

      {/* Delete Confirmation Dialog */}
      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete Sessions</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete {selectedCount} session{selectedCount !== 1 ? "s" : ""}?
              This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="mt-4">
            <DialogClose asChild>
              <Button variant="outline" disabled={bulkDeleteMutation.isPending}>
                Cancel
              </Button>
            </DialogClose>
            <LoadingButton
              variant="destructive"
              loading={bulkDeleteMutation.isPending}
              onClick={() => bulkDeleteMutation.mutate(selectedSessionIds)}
            >
              Delete {selectedCount} Session{selectedCount !== 1 ? "s" : ""}
            </LoadingButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
