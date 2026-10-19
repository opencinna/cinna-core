import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

/**
 * The loading state of the User Groups tables: the real header, five skeleton
 * rows, and an action slot at the end — the `PendingUsers` shape with the
 * caller's columns, so the table does not jump when the data lands.
 */
export function PendingTable({ headers }: { headers: string[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {headers.map((header) => (
            <TableHead key={header}>{header}</TableHead>
          ))}
          <TableHead>
            <span className="sr-only">Actions</span>
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {Array.from({ length: 5 }).map((_, index) => (
          <TableRow key={index}>
            {headers.map((header, column) => (
              <TableCell key={header}>
                <Skeleton className={column === 0 ? "h-4 w-40" : "h-4 w-20"} />
              </TableCell>
            ))}
            <TableCell>
              <div className="flex justify-end">
                <Skeleton className="size-7 rounded-md" />
              </div>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
