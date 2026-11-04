import { CircleAlert, TriangleAlert } from "lucide-react"

import { RowFlag } from "@/components/Common/ListRow"
import { cn } from "@/lib/utils"
import { isRecord } from "./componentSpec"

interface Metric {
  key: string
  label: string
  value: string
  unit: string | null
  delta: string | null
  /** Whether the delta is good news, when the metric says which way is good. */
  deltaGood: boolean | null
  status: "ok" | "warning" | "error" | null
}

const numberFormat = new Intl.NumberFormat(undefined, {
  maximumFractionDigits: 2,
})

function show(value: unknown): string {
  return typeof value === "number" ? numberFormat.format(value) : String(value)
}

export function readMetrics(data: unknown): Metric[] {
  const items = isRecord(data) && Array.isArray(data.items) ? data.items : []
  return items.filter(isRecord).map((item, index) => {
    const delta = item.delta
    const deltaNumber =
      typeof delta === "number"
        ? delta
        : typeof delta === "string"
          ? Number.parseFloat(delta)
          : Number.NaN
    const direction =
      item.delta_good === "up" || item.delta_good === "down"
        ? item.delta_good
        : null
    return {
      key:
        typeof item.id === "string" || typeof item.id === "number"
          ? String(item.id)
          : `metric-${index}`,
      label: typeof item.label === "string" ? item.label : "",
      value: show(item.value ?? ""),
      unit: typeof item.unit === "string" && item.unit ? item.unit : null,
      delta:
        delta === null || delta === undefined || delta === ""
          ? null
          : typeof delta === "number" && delta > 0
            ? `+${show(delta)}`
            : show(delta),
      deltaGood:
        direction && !Number.isNaN(deltaNumber) && deltaNumber !== 0
          ? deltaNumber > 0 === (direction === "up")
          : null,
      status:
        item.status === "ok" ||
        item.status === "warning" ||
        item.status === "error"
          ? item.status
          : null,
    }
  })
}

/** `view: "metrics"` — value over label, an optional delta in success/destructive. */
export function NativeMetricsView({ metrics }: { metrics: Metric[] }) {
  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(120px,1fr))] gap-2 p-2">
      {metrics.map((metric) => (
        <div
          key={metric.key}
          className="min-w-0 rounded-md bg-muted/30 px-2 py-1.5"
        >
          <div className="flex items-baseline gap-1 min-w-0">
            <span className="truncate text-2xl font-semibold tabular-nums">
              {metric.value}
            </span>
            {metric.unit && (
              <span className="shrink-0 text-xs text-muted-foreground">
                {metric.unit}
              </span>
            )}
          </div>
          <div className="flex min-w-0 items-center gap-1.5">
            <span className="truncate text-xs text-muted-foreground">
              {metric.label}
            </span>
            {metric.delta && (
              <span
                className={cn(
                  "shrink-0 text-xs tabular-nums",
                  metric.deltaGood === true && "text-success",
                  metric.deltaGood === false && "text-destructive",
                  metric.deltaGood === null && "text-muted-foreground",
                )}
              >
                {metric.delta}
              </span>
            )}
            {metric.status === "warning" && (
              <RowFlag
                icon={TriangleAlert}
                tone="warning"
                label="Needs attention"
              />
            )}
            {metric.status === "error" && (
              <RowFlag
                icon={CircleAlert}
                tone="error"
                label="Something is wrong"
              />
            )}
          </div>
        </div>
      ))}
    </div>
  )
}
