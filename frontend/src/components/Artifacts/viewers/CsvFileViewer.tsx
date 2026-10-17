import { useVirtualizer } from "@tanstack/react-virtual"
import { useMemo, useRef } from "react"

import { parseCsv } from "@/utils/csv"
import { CSV_ROW_LIMIT } from "./fileKinds"
import { ViewerMessage } from "./ViewerStates"

const ROW_HEIGHT = 28

interface CsvTableProps {
  text: string
  delimiter: string
  downloadHref: string
}

/**
 * CSV / TSV as a virtualized table: a sticky header row, a leading row
 * number, the first 10,000 rows, and a footer when the file has more.
 */
export function CsvTable({ text, delimiter, downloadHref }: CsvTableProps) {
  const { header, rows, totalRows } = useMemo(() => {
    const parsed = parseCsv(text, { delimiter, maxRows: CSV_ROW_LIMIT + 1 })
    return {
      header: parsed.rows[0] ?? [],
      rows: parsed.rows.slice(1),
      totalRows: Math.max(parsed.totalRows - 1, 0),
    }
  }, [text, delimiter])

  const scrollRef = useRef<HTMLDivElement>(null)
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 20,
  })

  if (header.length === 0) {
    return <ViewerMessage>This file is empty.</ViewerMessage>
  }

  const columnCount = Math.max(
    header.length,
    ...rows.slice(0, 200).map((r) => r.length),
  )
  const numberWidth = `${String(rows.length).length + 2}ch`
  const template = `${numberWidth} repeat(${columnCount}, minmax(7rem, 16rem))`
  const truncated = totalRows > rows.length

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto text-xs">
        <div
          className="sticky top-0 z-10 grid w-max min-w-full border-b bg-muted font-medium"
          style={{ gridTemplateColumns: template }}
        >
          <span className="px-2 py-1.5 text-muted-foreground">#</span>
          {Array.from({ length: columnCount }, (_, i) => (
            <span key={i} className="truncate px-2 py-1.5" title={header[i]}>
              {header[i] ?? ""}
            </span>
          ))}
        </div>
        <div
          className="relative w-max min-w-full"
          style={{ height: virtualizer.getTotalSize() }}
        >
          {virtualizer.getVirtualItems().map((item) => {
            const row = rows[item.index]
            return (
              <div
                key={item.key}
                className="absolute left-0 grid w-max min-w-full border-b border-border/60 hover:bg-muted/40"
                style={{
                  gridTemplateColumns: template,
                  height: ROW_HEIGHT,
                  transform: `translateY(${item.start}px)`,
                }}
              >
                <span className="px-2 py-1.5 tabular-nums text-muted-foreground">
                  {item.index + 1}
                </span>
                {Array.from({ length: columnCount }, (_, i) => (
                  <span key={i} className="truncate px-2 py-1.5" title={row[i]}>
                    {row[i] ?? ""}
                  </span>
                ))}
              </div>
            )
          })}
        </div>
      </div>
      {truncated && (
        <p className="shrink-0 border-t px-3 py-1.5 text-xs text-muted-foreground">
          Showing {rows.length.toLocaleString()} of {totalRows.toLocaleString()}{" "}
          rows ·{" "}
          <a href={downloadHref} className="underline-offset-4 hover:underline">
            Download the full file
          </a>
        </p>
      )}
    </div>
  )
}
