import { useMemo } from "react"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { parseCsv } from "@/utils/csv"

interface CSVViewerProps {
  content: string
}

export function CSVViewer({ content }: CSVViewerProps) {
  const { headers, rows } = useMemo(() => {
    const parsed = parseCsv(content).rows
    if (parsed.length === 0) {
      return { headers: [], rows: [] }
    }

    const headers = parsed[0]
    const rows = parsed.slice(1)

    return { headers, rows }
  }, [content])

  if (headers.length === 0) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-muted-foreground">Empty CSV file</p>
      </div>
    )
  }

  return (
    <div className="h-full flex flex-col overflow-auto">
      <div className="p-6">
        <Table>
          <TableHeader>
            <TableRow>
              {headers.map((header, index) => (
                <TableHead key={index} className="font-semibold whitespace-nowrap">
                  {header}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row, rowIndex) => (
              <TableRow key={rowIndex}>
                {row.map((cell, cellIndex) => (
                  <TableCell key={cellIndex} className="whitespace-nowrap">
                    {cell}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
