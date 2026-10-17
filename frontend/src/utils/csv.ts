export interface ParseCsvOptions {
  /** Cell separator: `,` for CSV, `\t` for TSV. */
  delimiter?: string
  /**
   * Stop after this many rows (the header row included). The parse still
   * counts the rest, so a caller can say "showing N of M".
   */
  maxRows?: number
}

export interface ParsedCsv {
  rows: string[][]
  /** Every row in the text, including the ones past `maxRows`. */
  totalRows: number
}

/**
 * RFC-4180-ish parser: quoted cells, `""` escapes, `\r\n` / `\n` / `\r` line
 * ends, blank lines skipped. Extracted from `Environment/CSVViewer` so the
 * artifact viewer can cap and count without a second parser.
 */
export function parseCsv(
  text: string,
  { delimiter = ",", maxRows = Number.POSITIVE_INFINITY }: ParseCsvOptions = {},
): ParsedCsv {
  const rows: string[][] = []
  let totalRows = 0
  let currentRow: string[] = []
  let currentCell = ""
  let insideQuotes = false

  const endRow = () => {
    currentRow.push(currentCell)
    if (totalRows < maxRows) rows.push(currentRow)
    totalRows += 1
    currentRow = []
    currentCell = ""
  }

  for (let i = 0; i < text.length; i++) {
    const char = text[i]
    const nextChar = text[i + 1]

    if (char === '"') {
      if (insideQuotes && nextChar === '"') {
        currentCell += '"'
        i++
      } else {
        insideQuotes = !insideQuotes
      }
    } else if (char === delimiter && !insideQuotes) {
      currentRow.push(currentCell)
      currentCell = ""
    } else if ((char === "\n" || char === "\r") && !insideQuotes) {
      if (char === "\r" && nextChar === "\n") i++
      if (currentCell || currentRow.length > 0) endRow()
    } else {
      currentCell += char
    }
  }

  if (currentCell || currentRow.length > 0) endRow()

  return { rows, totalRows }
}
