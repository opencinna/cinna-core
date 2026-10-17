import { ARTIFACT_COLUMN_CLASSES as COL } from "@/components/Artifacts/artifactColumns"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

/** The Artifacts table while it loads: its real columns, its real widths. */
const PendingArtifacts = () => (
  <Table className="table-fixed">
    <TableHeader className="bg-transparent [&_tr]:border-b-0 [&_th]:h-0 [&_th]:py-0">
      <TableRow className="hover:bg-transparent">
        <TableHead>
          <span className="sr-only">Name</span>
        </TableHead>
        <TableHead className={COL.source}>
          <span className="sr-only">Source</span>
        </TableHead>
        <TableHead className={COL.updated}>
          <span className="sr-only">Updated</span>
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
              <Skeleton className="h-4 w-48" />
            </div>
          </TableCell>
          <TableCell className={COL.source}>
            <Skeleton className="h-3 w-32" />
          </TableCell>
          <TableCell className={COL.updated}>
            <Skeleton className="h-3 w-20" />
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

export default PendingArtifacts
