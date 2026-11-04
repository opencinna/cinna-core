import { ListRow, ListRowGroup, RowInfo } from "@/components/Common/ListRow"
import { formatRelativeTimestamp } from "@/components/Common/RelativeTime"
import { Badge } from "@/components/ui/badge"
import { isRecord } from "./componentSpec"
import {
  isSafeUrl,
  rowId,
  rowStateClass,
  rowToggleHandlers,
  useScrollIntoViewWhen,
} from "./nativeRows"

interface ListItem {
  id: string | null
  title: string
  subtitle: string | null
  meta: string | null
  time: string | null
  priority: string | null
  badge: string | null
  url: string | null
}

function str(value: unknown): string | null {
  return typeof value === "string" && value ? value : null
}

export function readListItems(data: unknown): ListItem[] {
  const items = isRecord(data) && Array.isArray(data.items) ? data.items : []
  return items.filter(isRecord).map((item) => ({
    id: rowId(item.id),
    title: str(item.title) || "",
    subtitle: str(item.subtitle),
    meta: str(item.meta),
    time: str(item.time),
    priority: str(item.priority),
    badge: str(item.badge),
    url: isSafeUrl(item.url) ? item.url : null,
  }))
}

interface Props {
  items: ListItem[]
  selection: ReadonlySet<string>
  onToggle: (id: string) => void
  highlighted: ReadonlySet<string>
  currentId: string | null
}

/**
 * `view: "list"` — P3 rows (`ListRow`), no menu: the title (a plain link when
 * the item has a `url`), ≤ 1 badge, the subtitle as the metadata line only
 * when there is one, and the remaining facts in `RowInfo`.
 */
export function NativeListView({
  items,
  selection,
  onToggle,
  highlighted,
  currentId,
}: Props) {
  return (
    <ListRowGroup
      // `pr-2`: the rightmost RowInfo must not sit under the body scrollbar.
      className="mx-0 pr-2"
      role="listbox"
      aria-multiselectable="true"
      aria-label="Items"
    >
      {items.map((item, index) => (
        <ListItemRow
          key={item.id ?? `row-${index}`}
          item={item}
          selected={!!item.id && selection.has(item.id)}
          highlighted={!!item.id && highlighted.has(item.id)}
          current={!!item.id && item.id === currentId}
          onToggle={onToggle}
        />
      ))}
    </ListRowGroup>
  )
}

function ListItemRow({
  item,
  selected,
  highlighted,
  current,
  onToggle,
}: {
  item: ListItem
  selected: boolean
  highlighted: boolean
  current: boolean
  onToggle: (id: string) => void
}) {
  const ref = useScrollIntoViewWhen<HTMLDivElement>(current)
  const id = item.id
  const time = item.time ? formatRelativeTimestamp(item.time) : null
  const title = item.url ? (
    <a
      href={item.url}
      target={item.url.startsWith("mailto:") ? undefined : "_blank"}
      rel="noopener noreferrer"
      className="hover:underline"
    >
      {item.title}
    </a>
  ) : (
    item.title
  )

  const row = (
    <ListRow
      title={title}
      badges={
        item.badge ? (
          <Badge variant="outline" className="h-5 shrink-0">
            {item.badge}
          </Badge>
        ) : undefined
      }
      meta={item.subtitle || undefined}
      flags={
        <RowInfo
          facts={[
            item.meta,
            time,
            item.priority && `Priority: ${item.priority}`,
          ]}
        />
      }
      className={rowStateClass({
        selectable: !!id,
        selected,
        highlighted,
        current,
      })}
    />
  )

  if (!id) return <div ref={ref}>{row}</div>

  return (
    <div
      ref={ref}
      role="option"
      aria-selected={selected}
      tabIndex={0}
      className="outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
      {...rowToggleHandlers(() => onToggle(id))}
    >
      {row}
    </div>
  )
}
