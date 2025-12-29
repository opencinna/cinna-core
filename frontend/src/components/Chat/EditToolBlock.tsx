import { FileEdit, ChevronDown, ChevronUp } from "lucide-react"
import { useState, useMemo } from "react"

interface EditToolBlockProps {
  filePath: string
  oldString: string
  newString: string
}

const MAX_PREVIEW_LINES = 5

export function EditToolBlock({ filePath, oldString, newString }: EditToolBlockProps) {
  const [activeTab, setActiveTab] = useState<'new' | 'old'>('new')
  const [isExpanded, setIsExpanded] = useState(false)

  const currentContent = activeTab === 'new' ? newString : oldString

  const { previewContent, hasMore, totalLines } = useMemo(() => {
    const lines = currentContent.split('\n')
    const totalLines = lines.length
    const hasMore = totalLines > MAX_PREVIEW_LINES
    const previewContent = hasMore && !isExpanded
      ? lines.slice(0, MAX_PREVIEW_LINES).join('\n')
      : currentContent

    return { previewContent, hasMore, totalLines }
  }, [currentContent, isExpanded])

  return (
    <div className="flex items-start gap-2 text-sm bg-slate-100 dark:bg-slate-800 border border-border rounded px-3 py-2">
      <FileEdit className="h-4 w-4 text-muted-foreground mt-0.5 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <div className="text-foreground/90 mb-2">
          Editing file{" "}
          <code className="font-mono bg-slate-200 dark:bg-slate-700 px-1.5 py-0.5 rounded text-xs">
            {filePath}
          </code>
        </div>

        {/* Tab Headers - CLI style */}
        <div className="flex gap-1 border-b border-border">
          <button
            onClick={() => setActiveTab('new')}
            className={`px-3 py-1.5 text-xs font-mono transition-colors rounded-t ${
              activeTab === 'new'
                ? 'text-foreground bg-background'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            New version
          </button>
          <button
            onClick={() => setActiveTab('old')}
            className={`px-3 py-1.5 text-xs font-mono transition-colors rounded-t ${
              activeTab === 'old'
                ? 'text-foreground bg-background'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            Previous version
          </button>
        </div>

        {/* Tab Content */}
        <div className="bg-background border border-border rounded p-2">
          <pre className="text-xs text-foreground/80 overflow-x-auto whitespace-pre-wrap break-words font-mono">
            {previewContent}
          </pre>

          {hasMore && (
            <button
              onClick={() => setIsExpanded(!isExpanded)}
              className="mt-2 flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
            >
              {isExpanded ? (
                <>
                  <ChevronUp className="h-3 w-3" />
                  Show less
                </>
              ) : (
                <>
                  <ChevronDown className="h-3 w-3" />
                  Show all ({totalLines} lines)
                </>
              )}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}