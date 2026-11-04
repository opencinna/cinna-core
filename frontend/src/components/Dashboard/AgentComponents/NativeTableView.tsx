import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { isRecord } from "./componentSpec"
import {
  isSafeUrl,
  rowId,
  rowStateClass,
  rowToggleHandlers,
  useScrollIntoViewWhen,
} from "./nativeRows"

type ColumnType = "text" | "number" | "money" | "percent" | "date" | "badge"

interface Column {
  key: string
  label: string
  type: ColumnType
}

export interface TableData {
  columns: Column[]
  rows: Array<{ id: string | null; cells: Record<string, unknown> }>
}

const TYPES: readonly ColumnType[] = [
  "text",
  "number",
  "money",
  "percent",
  "date",
  "badge",
]

export function readTable(data: unknown): TableData {
  if (!isRecord(data)) return { columns: [], rows: [] }
  const columns = (Array.isArray(data.columns) ? data.columns : [])
    .filter(isRecord)
    .filter((c) => typeof c.key === "string" && c.key)
    .map((c) => ({
      key: c.key as string,
      label: (typeof c.label === "string" && c.label) || (c.key as string),
      type: TYPES.includes(c.type as ColumnType)
        ? (c.type as ColumnType)
        : "text",
    }))
  const rows = (Array.isArray(data.rows) ? data.rows : [])
    .filter(isRecord)
    .map((r) => ({ id: rowId(r.id), cells: r }))
  return { columns, rows }
}

const numberFormat = new Intl.NumberFormat(undefined, {
  maximumFractionDigits: 2,
})
const moneyFormat = new Intl.NumberFormat(undefined, {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

function Cell({ value, type }: { value: unknown; type: ColumnType }) {
  if (value === null || value === undefined || value === "") return null
  if (type === "badge") {
    return (
      <Badge variant="outline" className="h-5">
        {String(value)}
      </Badge>
    )
  }
  if (typeof value === "number") {
    if (type === "money") return <>{moneyFormat.format(value)}</>
    if (type === "percent") return <>{numberFormat.format(value)}%</>
    return <>{numberFormat.format(value)}</>
  }
  if (isSafeUrl(value)) {
    return (
      <a
        href={value}
        target={value.startsWith("mailto:") ? undefined : "_blank"}
        rel="noopener noreferrer"
        className="hover:underline"
      >
        {value}
      </a>
    )
  }
  return (
    <>{typeof value === "object" ? JSON.stringify(value) : String(value)}</>
  )
}

const NUMERIC: ReadonlySet<ColumnType> = new Set(["number", "money", "percent"])

interface Props {
  table: TableData
  selection: ReadonlySet<string>
  onToggle: (id: string) => void
  highlighted: ReadonlySet<string>
  currentId: string | null
}

/**
 * `view: "table"` — a plain `text-xs` table with a sticky header. The tile body
 * scrolls in both axes; the table never widens the tile.
 */
export function NativeTableView({
  table,
  selection,
  onToggle,
  highlighted,
  currentId,
}: Props) {
  return (
    // biome-ignore lint/a11y/useSemanticElements: the element is already a <table>; the role adds selection, which a plain table has no way to announce.
    <table
      // biome-ignore lint/a11y/noNoninteractiveElementToInteractiveRole: multi-selectable rows are a grid with aria-selected rows in ARIA.
      role="grid"
      aria-multiselectable="true"
      aria-label="Rows"
      className="w-full caption-bottom text-xs"
    >
      <thead className="sticky top-0 z-[1] bg-background shadow-[inset_0_-1px_0_var(--border)]">
        <tr>
          {table.columns.map((column) => (
            <th
              key={column.key}
              scope="col"
              className={cn(
                "h-7 whitespace-nowrap px-2 text-left font-medium text-muted-foreground",
                NUMERIC.has(column.type) && "text-right",
              )}
            >
              {column.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {table.rows.map((row, index) => (
          <TableRow
            key={row.id ?? `row-${index}`}
            row={row}
            columns={table.columns}
            selected={!!row.id && selection.has(row.id)}
            highlighted={!!row.id && highlighted.has(row.id)}
            current={!!row.id && row.id === currentId}
            onToggle={onToggle}
          />
        ))}
      </tbody>
    </table>
  )
}

function TableRow({
  row,
  columns,
  selected,
  highlighted,
  current,
  onToggle,
}: {
  row: TableData["rows"][number]
  columns: Column[]
  selected: boolean
  highlighted: boolean
  current: boolean
  onToggle: (id: string) => void
}) {
  const ref = useScrollIntoViewWhen<HTMLTableRowElement>(current)
  const id = row.id
  const selectable = !!id
  return (
    <tr
      ref={ref}
      aria-selected={selectable ? selected : undefined}
      tabIndex={selectable ? 0 : undefined}
      className={cn(
        "border-b border-border transition-colors hover:bg-muted/40 outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
        rowStateClass({ selectable, selected, highlighted, current }),
      )}
      {...(id ? rowToggleHandlers(() => onToggle(id)) : {})}
    >
      {columns.map((column) => (
        <td
          key={column.key}
          className={cn(
            "max-w-[24rem] truncate px-2 py-1.5 align-middle",
            NUMERIC.has(column.type) && "text-right tabular-nums",
          )}
        >
          <Cell value={row.cells[column.key]} type={column.type} />
        </td>
      ))}
    </tr>
  )
}
