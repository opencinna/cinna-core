import { useCallback } from "react"
import { defaultUrlTransform } from "react-markdown"

import { MarkdownViewer } from "@/components/Environment/MarkdownViewer"
import { fileUrl, pathFromUrl } from "./fileKinds"
import { TextFileGate } from "./ViewerStates"

/** A URL with a scheme (`https:`, `mailto:`), protocol-relative, or a fragment. */
const NOT_RELATIVE = /^(?:[a-z][a-z\d+.-]*:|\/\/|#)/i

/**
 * Resolve a link written in `fromPath` to a path inside the artifact.
 * `/x` is the artifact's root; `../` above the root stays at the root.
 */
function resolvePath(fromPath: string, target: string): string {
  const parts = target.startsWith("/") ? [] : fromPath.split("/").slice(0, -1)
  for (const segment of target.split("/")) {
    if (segment === "" || segment === ".") continue
    if (segment === "..") parts.pop()
    else parts.push(segment)
  }
  return parts.join("/")
}

interface MarkdownFileViewerProps {
  contentBase: string
  path: string
  name: string
  /** Manifest paths: a link to one of them selects it instead of leaving. */
  paths: Set<string>
  onSelectFile: (path: string) => void
}

/**
 * Markdown through `Environment/MarkdownViewer` (no raw HTML), with relative
 * links and images pointed at this revision's capability URLs.
 */
export function MarkdownFileViewer({
  contentBase,
  path,
  name,
  paths,
  onSelectFile,
}: MarkdownFileViewerProps) {
  const urlTransform = useCallback(
    (url: string) => {
      if (NOT_RELATIVE.test(url)) return defaultUrlTransform(url)
      const [target, ...hash] = url.split("#")
      const [pathPart] = target.split("?")
      let decoded = pathPart
      try {
        decoded = decodeURIComponent(pathPart)
      } catch {
        // A stray `%` stays literal.
      }
      const resolved = resolvePath(path, decoded)
      const suffix = hash.length > 0 ? `#${hash.join("#")}` : ""
      return defaultUrlTransform(fileUrl(contentBase, resolved) + suffix)
    },
    [contentBase, path],
  )

  const onLinkClick = useCallback(
    (href: string) => {
      const target = pathFromUrl(contentBase, href)
      if (target && paths.has(target)) {
        onSelectFile(target)
        return true
      }
      // Anything else leaves in a new tab, so a recipient on /s/ keeps the page.
      if (/^https?:/i.test(href)) {
        window.open(href, "_blank", "noopener,noreferrer")
        return true
      }
      return false
    },
    [contentBase, paths, onSelectFile],
  )

  return (
    <TextFileGate url={fileUrl(contentBase, path)} name={name}>
      {(text) => (
        <MarkdownViewer
          content={text}
          className="mx-auto max-w-3xl"
          urlTransform={urlTransform}
          onLinkClick={onLinkClick}
        />
      )}
    </TextFileGate>
  )
}
