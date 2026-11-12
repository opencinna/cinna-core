/**
 * The Endpoint tab of an API capability request: what the caller proposed —
 * method and path, parameters, response fields — and their example request and
 * response.
 *
 * Everything here is caller-authored and shown as plain text: no markdown, no
 * links, no copy buttons. Wide tables and long examples scroll inside their
 * own pane, never the dialog.
 */
import type { ReactNode } from "react"

import { Badge } from "@/components/ui/badge"
import type { ApiCapabilityRequest } from "@/utils/improvementRequests"

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="min-w-0 space-y-2">
      <h4 className="text-sm font-medium">{title}</h4>
      {children}
    </section>
  )
}

function FieldTable({
  columns,
  rows,
}: {
  columns: string[]
  rows: string[][]
}) {
  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full text-sm">
        <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
          <tr>
            {columns.map((column) => (
              <th key={column} className="px-3 py-1.5 font-medium">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y">
          {rows.map((cells, index) => (
            // Caller-authored rows may repeat a name, so the position keys.
            <tr key={index} className="align-top">
              {cells.map((cell, cellIndex) => (
                <td
                  key={cellIndex}
                  className={
                    cellIndex === 0
                      ? "whitespace-nowrap px-3 py-1.5 font-mono text-xs"
                      : cellIndex === cells.length - 1
                        ? "min-w-[12rem] break-words px-3 py-1.5"
                        : "whitespace-nowrap px-3 py-1.5 text-muted-foreground"
                  }
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Example({ text }: { text: string }) {
  return (
    <pre className="max-h-60 overflow-auto whitespace-pre rounded-md border bg-muted/40 p-3 font-mono text-xs">
      {text}
    </pre>
  )
}

export function ImprovementRequestApiEndpoint({
  payload,
}: {
  payload: ApiCapabilityRequest
}) {
  return (
    <div className="min-w-0 space-y-4">
      {(payload.method || payload.path) && (
        <Section title="Endpoint">
          <div className="flex min-w-0 items-start gap-2">
            {payload.method && (
              <Badge variant="outline" className="h-5 shrink-0 font-mono">
                {payload.method.toUpperCase()}
              </Badge>
            )}
            {payload.path && (
              <span className="min-w-0 break-all font-mono text-sm">
                {payload.path}
              </span>
            )}
          </div>
        </Section>
      )}
      {payload.params.length > 0 && (
        <Section title="Parameters">
          <FieldTable
            columns={["Name", "In", "Type", "Description"]}
            rows={payload.params.map((p) => [
              p.name,
              p.in,
              p.type,
              p.description,
            ])}
          />
        </Section>
      )}
      {payload.responseFields.length > 0 && (
        <Section title="Response fields">
          <FieldTable
            columns={["Name", "Type", "Description"]}
            rows={payload.responseFields.map((f) => [
              f.name,
              f.type,
              f.description,
            ])}
          />
        </Section>
      )}
      {payload.exampleRequest && (
        <Section title="Example request">
          <Example text={payload.exampleRequest} />
        </Section>
      )}
      {payload.exampleResponse && (
        <Section title="Example response">
          <Example text={payload.exampleResponse} />
        </Section>
      )}
    </div>
  )
}
