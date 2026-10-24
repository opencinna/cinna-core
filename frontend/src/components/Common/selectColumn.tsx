import type { ColumnDef } from "@tanstack/react-table"

import { TriStateCheckbox } from "@/components/Common/TriStateCheckbox"
import { Checkbox } from "@/components/ui/checkbox"

/**
 * The checkbox column of a selectable `DataTable` (pass `rowSelection` /
 * `onRowSelectionChange` to the table). The header selects the current page
 * only; on a server-paged list that is all the table can see.
 *
 * Extracted from `Admin/Environments/AdminEnvTable.tsx` at its second
 * consumer. `rowLabel` names the row for screen readers: a column of
 * identical "Select row" checkboxes says nothing.
 */
export function selectColumn<T>({
  rowLabel,
  canSelect,
}: {
  rowLabel: (row: T) => string
  canSelect?: (row: T) => boolean
}): ColumnDef<T> {
  return {
    id: "select",
    meta: { className: "w-8" },
    header: ({ table }) => (
      <TriStateCheckbox
        checked={
          table.getIsAllPageRowsSelected() ||
          (table.getIsSomePageRowsSelected() && "indeterminate")
        }
        onCheckedChange={(value) => table.toggleAllPageRowsSelected(!!value)}
        aria-label="Select all on this page"
      />
    ),
    cell: ({ row }) => (
      <Checkbox
        checked={row.getIsSelected()}
        onCheckedChange={(value) => row.toggleSelected(!!value)}
        disabled={canSelect ? !canSelect(row.original) : undefined}
        aria-label={`Select ${rowLabel(row.original)}`}
      />
    ),
  }
}
