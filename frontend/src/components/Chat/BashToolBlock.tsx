import { Terminal } from "lucide-react"

interface BashToolBlockProps {
  command: string
}

export function BashToolBlock({ command }: BashToolBlockProps) {
  return (
    <div className="flex items-start gap-2 text-sm bg-slate-100 dark:bg-slate-800 border border-border rounded px-3 py-2">
      <Terminal className="h-4 w-4 text-muted-foreground mt-0.5 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <span className="text-foreground/90">
          Executing{" "}
          <code className="font-mono bg-slate-200 dark:bg-slate-700 px-1.5 py-0.5 rounded text-xs">
            bash
          </code>
          :{" "}
          <code className="font-mono bg-slate-200 dark:bg-slate-700 px-1.5 py-0.5 rounded text-xs">
            {command}
          </code>
        </span>
      </div>
    </div>
  )
}
