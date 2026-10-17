import { ChevronDown, ChevronRight, Folder, FolderOpen } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import type { ArtifactFileEntry } from "@/client"
import { FileIcon } from "@/components/Environment/FileIcon"
import { cn } from "@/lib/utils"
import { extensionOf } from "./fileKinds"

interface TreeNode {
  name: string
  /** Full path; for a folder, its prefix without the trailing slash. */
  path: string
  children: TreeNode[] | null
}

/** Folders first, then files, each by name. */
function buildTree(files: ArtifactFileEntry[]): TreeNode[] {
  const root: TreeNode = { name: "", path: "", children: [] }
  for (const file of files) {
    const segments = file.path.split("/")
    let node = root
    segments.forEach((segment, i) => {
      const isFile = i === segments.length - 1
      const path = segments.slice(0, i + 1).join("/")
      const children = node.children as TreeNode[]
      let next = children.find(
        (c) => c.name === segment && (c.children === null) === isFile,
      )
      if (!next) {
        next = { name: segment, path, children: isFile ? null : [] }
        children.push(next)
      }
      node = next
    })
  }
  const sort = (nodes: TreeNode[]) => {
    nodes.sort((a, b) => {
      if ((a.children === null) !== (b.children === null)) {
        return a.children === null ? 1 : -1
      }
      return a.name.localeCompare(b.name, undefined, { numeric: true })
    })
    for (const n of nodes) if (n.children) sort(n.children)
  }
  sort(root.children as TreeNode[])
  return root.children as TreeNode[]
}

/** The folders that hold `path`, so the selected file is never hidden. */
function ancestorsOf(path: string | null): string[] {
  if (!path) return []
  const segments = path.split("/")
  return segments.slice(0, -1).map((_, i) => segments.slice(0, i + 1).join("/"))
}

interface ArtifactFileTreeProps {
  files: ArtifactFileEntry[]
  selected: string | null
  onSelect: (path: string) => void
}

/**
 * The files browser's tree (S5): rows in the `Environment/TreeItemRenderer`
 * classes, root level open, deeper folders closed; no per-row actions —
 * downloading lives in the file bar.
 */
export function ArtifactFileTree({
  files,
  selected,
  onSelect,
}: ArtifactFileTreeProps) {
  const tree = useMemo(() => buildTree(files), [files])
  const [expanded, setExpanded] = useState<Set<string>>(
    () => new Set(ancestorsOf(selected)),
  )

  // A file selected from elsewhere (a markdown link, Back) is revealed.
  useEffect(() => {
    const ancestors = ancestorsOf(selected)
    if (ancestors.length === 0) return
    setExpanded((prev) =>
      ancestors.every((a) => prev.has(a))
        ? prev
        : new Set([...prev, ...ancestors]),
    )
  }, [selected])

  const toggle = (path: string) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })

  const renderNodes = (nodes: TreeNode[], level: number) =>
    nodes.map((node) => {
      const paddingLeft = `${level * 12 + 8}px`
      if (node.children) {
        const open = expanded.has(node.path)
        return (
          <li key={`d:${node.path}`}>
            <button
              type="button"
              aria-expanded={open}
              onClick={() => toggle(node.path)}
              className="flex w-full items-center gap-2 rounded-md py-1 pr-2 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
              style={{ paddingLeft }}
            >
              {open ? (
                <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" />
              ) : (
                <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground" />
              )}
              {open ? (
                <FolderOpen className="h-4 w-4 shrink-0 text-muted-foreground" />
              ) : (
                <Folder className="h-4 w-4 shrink-0 text-muted-foreground" />
              )}
              <span className="truncate text-sm font-medium">{node.name}</span>
            </button>
            {open && <ul>{renderNodes(node.children, level + 1)}</ul>}
          </li>
        )
      }
      const isSelected = node.path === selected
      return (
        <li key={`f:${node.path}`}>
          <button
            type="button"
            aria-current={isSelected ? "true" : undefined}
            onClick={() => onSelect(node.path)}
            className={cn(
              "flex w-full items-center gap-2 rounded-md py-1 pr-2 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
              isSelected && "bg-muted",
            )}
            // Files line up with the folder names beside their chevrons.
            style={{ paddingLeft: `${level * 12 + 28}px` }}
          >
            <FileIcon
              fileType={extensionOf(node.path)}
              className="h-4 w-4 shrink-0"
            />
            <span className="truncate text-sm">{node.name}</span>
          </button>
        </li>
      )
    })

  return (
    <nav aria-label="Files" className="h-full overflow-y-auto py-2 pr-2 pl-2">
      <ul>{renderNodes(tree, 0)}</ul>
    </nav>
  )
}
