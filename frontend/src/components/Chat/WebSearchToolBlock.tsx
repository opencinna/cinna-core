import { Search } from "lucide-react"

interface WebSearchToolBlockProps {
  query: string
}

export function WebSearchToolBlock({ query }: WebSearchToolBlockProps) {
  const searchUrl = `https://duckduckgo.com/?q=${encodeURIComponent(query)}`

  return (
    <div className="flex items-start gap-2 text-sm bg-slate-100 dark:bg-slate-800 border border-border rounded px-3 py-2">
      <Search className="h-4 w-4 text-muted-foreground mt-0.5 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <span className="text-foreground/90">
          Searching web for{" "}
          <a
            href={searchUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="font-mono bg-muted px-1.5 py-0.5 rounded text-xs hover:bg-muted/80 transition-colors underline decoration-dotted underline-offset-2 cursor-pointer"
          >
            {query}
          </a>
        </span>
      </div>
    </div>
  )
}