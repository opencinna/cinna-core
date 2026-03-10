import { AppWindow } from "lucide-react"

interface WebappActionBlockProps {
  action: string
  data?: Record<string, unknown>
  isCompact?: boolean
}

export function WebappActionBlock({ action, data, isCompact }: WebappActionBlockProps) {
  if (isCompact) {
    return (
      <div className="inline-flex items-center gap-2 text-sm text-muted-foreground/80 mb-1">
        <AppWindow className="h-3.5 w-3.5 flex-shrink-0" />
        <span>Webapp: <code className="font-mono bg-muted px-1 py-0.5 rounded text-xs">{action}</code></span>
      </div>
    )
  }

  const hasData = data && Object.keys(data).length > 0

  return (
    <div className="flex items-start gap-2 text-sm bg-slate-100 dark:bg-slate-800 border border-border rounded px-3 py-2">
      <AppWindow className="h-4 w-4 text-muted-foreground mt-0.5 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <span className="text-foreground/90">
          Webapp action:{" "}
          <code className="font-mono bg-slate-200 dark:bg-slate-700 px-1.5 py-0.5 rounded text-xs">
            {action}
          </code>
        </span>
        {hasData && (
          <div className="mt-1 space-y-0.5 text-xs text-foreground/70">
            {Object.entries(data).map(([key, value]) => (
              <div key={key} className="flex gap-1">
                <span className="font-semibold text-foreground/80 shrink-0">{key}:</span>
                <span className="font-mono break-all">
                  {typeof value === "string" ? value : JSON.stringify(value)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
