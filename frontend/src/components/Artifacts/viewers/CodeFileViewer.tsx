import { type ReactNode, useEffect, useMemo, useState } from "react"

import { HIGHLIGHT_LIMIT } from "./fileKinds"

type Highlight = typeof import("./codeHighlight").highlightCode

/**
 * A code file, coloured by `language` once the highlighter has loaded. Until
 * then — and for files over HIGHLIGHT_LIMIT — the same frame shows the plain
 * text, so the swap changes colours only, never layout. The frame matches
 * Environment/TextViewer.
 */
export function CodeFileViewer({
  content,
  language,
}: {
  content: string
  language: string
}) {
  const highlightable = content.length <= HIGHLIGHT_LIMIT
  const [highlight, setHighlight] = useState<Highlight | null>(null)

  useEffect(() => {
    if (!highlightable) return
    let live = true
    import("./codeHighlight")
      .then((mod) => {
        if (live) setHighlight(() => mod.highlightCode)
      })
      .catch(() => {
        // The chunk failed to load: the plain text stays, which is still the file.
      })
    return () => {
      live = false
    }
  }, [highlightable])

  const body = useMemo<ReactNode>(() => {
    if (!highlight || !highlightable) return content
    try {
      return highlight(content, language)
    } catch {
      return content
    }
  }, [highlight, highlightable, content, language])

  if (!content.trim()) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-muted-foreground">Empty file</p>
      </div>
    )
  }

  return (
    <div className="h-full overflow-auto">
      <pre className="p-6 text-sm font-mono whitespace-pre-wrap break-words">
        {body}
      </pre>
    </div>
  )
}
