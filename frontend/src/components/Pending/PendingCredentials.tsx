import { CREDENTIAL_COLUMN_CLASSES as COL } from "@/components/Credentials/credentialColumns"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

/** The Credentials table while it loads: its real headers, its real widths. */
const PendingCredentials = () => (
  <Table className="table-fixed">
    {/* Collapsed like the real table's header: zero height, but it still
        sizes the columns of the fixed layout. */}
    <TableHeader className="bg-transparent [&_tr]:border-b-0 [&_th]:h-0 [&_th]:py-0">
      <TableRow className="hover:bg-transparent">
        <TableHead>
          <span className="sr-only">Name</span>
        </TableHead>
        <TableHead className={COL.facts}>
          <span className="sr-only">Type, sharing and agents</span>
        </TableHead>
        <TableHead className={COL.actions}>
          <span className="sr-only">Actions</span>
        </TableHead>
      </TableRow>
    </TableHeader>
    <TableBody>
      {Array.from({ length: 8 }).map((_, index) => (
        <TableRow key={index} className="hover:bg-transparent">
          <TableCell>
            <div className="flex items-center gap-2">
              <Skeleton className="h-4 w-4 shrink-0" />
              <Skeleton className="h-4 w-40" />
            </div>
          </TableCell>
          <TableCell className={COL.facts}>
            <div className="flex justify-end gap-1.5">
              <Skeleton className="h-5 w-24 rounded-full" />
              <Skeleton className="hidden h-5 w-10 rounded-full lg:block" />
            </div>
          </TableCell>
          <TableCell className={COL.actions}>
            <div className="flex justify-end">
              <Skeleton className="h-7 w-7 rounded-md" />
            </div>
          </TableCell>
        </TableRow>
      ))}
    </TableBody>
  </Table>
)

export default PendingCredentials
