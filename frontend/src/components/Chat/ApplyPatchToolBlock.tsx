import { FilePlus, FileEdit, FileMinus, FileText, ChevronDown, ChevronUp } from "lucide-react"
import { useState, useMemo } from "react"

/**
 * Renders the OpenCode `apply_patch` tool's `patch_text` parameter as a proper
 * diff instead of letting the generic tool renderer markdown-flatten it (which
 * collapses newlines and mangles the `***`/`+`/`-` syntax).
 *
 * Parses the codex-style envelope:
 *
 *   *** Begin Patch
 *   *** Add File: path        (body is all `+` lines)
 *   *** Update File: path     (body is `@@` hunks with ` `/`-`/`+` lines)
 *   *** Move to: newpath
 *   *** Delete File: path     (no body)
 *   *** End Patch
 *
 * Multiple file sections per patch are supported. If the text doesn't parse as
 * a recognizable patch, we fall back to a raw monospace block so nothing is
 * ever lost.
 */

interface ApplyPatchToolBlockProps {
  patchText: string
  isCompact?: boolean
}

type LineKind = "add" | "del" | "context" | "hunk"
type FileOp = "add" | "update" | "delete"

interface PatchLine {
  kind: LineKind
  text: string
}

interface PatchFile {
  op: FileOp
  path: string
  moveTo?: string
  lines: PatchLine[]
}

const MAX_PREVIEW_LINES = 8

const OP_META: Record<FileOp, { label: string; Icon: typeof FileText; badge: string }> = {
  add: {
    label: "Add",
    Icon: FilePlus,
    badge: "text-green-700 dark:text-green-400 bg-green-500/10 border-green-500/30",
  },
  update: {
    label: "Update",
    Icon: FileEdit,
    badge: "text-blue-700 dark:text-blue-400 bg-blue-500/10 border-blue-500/30",
  },
  delete: {
    label: "Delete",
    Icon: FileMinus,
    badge: "text-red-700 dark:text-red-400 bg-red-500/10 border-red-500/30",
  },
}

const FILE_HEADER_RE = /^\*\*\*\s+(Add|Update|Delete)\s+File:\s*(.+)$/
const MOVE_RE = /^\*\*\*\s+Move\s+to:\s*(.+)$/

function parsePatch(patchText: string): PatchFile[] | null {
  const rawLines = patchText.replace(/\r\n/g, "\n").split("\n")
  const files: PatchFile[] = []
  let current: PatchFile | null = null

  for (const line of rawLines) {
    const trimmed = line.trim()

    if (trimmed === "*** Begin Patch" || trimmed === "*** End Patch") {
      continue
    }

    const header = line.match(FILE_HEADER_RE)
    if (header) {
      const op = header[1].toLowerCase() as FileOp
      current = { op, path: header[2].trim(), lines: [] }
      files.push(current)
      continue
    }

    const move = line.match(MOVE_RE)
    if (move && current) {
      current.moveTo = move[1].trim()
      continue
    }

    if (!current) {
      // Content before any file header → not a recognizable patch.
      continue
    }

    // Hunk header (Update sections).
    if (line.startsWith("@@")) {
      current.lines.push({ kind: "hunk", text: line })
      continue
    }

    // Diff body lines: classify by the first character. Empty lines are context.
    const marker = line.charAt(0)
    if (marker === "+") {
      current.lines.push({ kind: "add", text: line.slice(1) })
    } else if (marker === "-") {
      current.lines.push({ kind: "del", text: line.slice(1) })
    } else {
      // Leading space = context; bare empty line = context too.
      current.lines.push({ kind: "context", text: marker === " " ? line.slice(1) : line })
    }
  }

  if (files.length === 0) return null
  return files
}

function lineClass(kind: LineKind): string {
  switch (kind) {
    case "add":
      return "text-green-700 dark:text-green-400 bg-green-500/10"
    case "del":
      return "text-red-700 dark:text-red-400 bg-red-500/10"
    case "hunk":
      return "text-blue-600 dark:text-blue-400 bg-muted/40"
    default:
      return "text-foreground/60"
  }
}

function linePrefix(kind: LineKind): string {
  switch (kind) {
    case "add":
      return "+"
    case "del":
      return "-"
    case "hunk":
      return ""
    default:
      return " "
  }
}

function FileDiff({ file }: { file: PatchFile }) {
  const [isExpanded, setIsExpanded] = useState(false)
  const { Icon, label, badge } = OP_META[file.op]

  const additions = file.lines.filter((l) => l.kind === "add").length
  const deletions = file.lines.filter((l) => l.kind === "del").length

  const hasMore = file.lines.length > MAX_PREVIEW_LINES
  const visibleLines =
    hasMore && !isExpanded ? file.lines.slice(0, MAX_PREVIEW_LINES) : file.lines

  return (
    <div className="border border-border rounded overflow-hidden">
      <div className="flex items-center gap-2 px-2 py-1.5 bg-muted/40 text-xs">
        <Icon className="h-3.5 w-3.5 flex-shrink-0 text-muted-foreground" />
        <span className={`px-1.5 py-0.5 rounded border font-medium ${badge}`}>{label}</span>
        <code className="font-mono text-foreground/80 truncate" title={file.path}>
          {file.path}
        </code>
        {file.moveTo && (
          <code className="font-mono text-muted-foreground truncate" title={file.moveTo}>
            → {file.moveTo}
          </code>
        )}
        <span className="ml-auto flex items-center gap-2 font-mono text-[11px]">
          {additions > 0 && <span className="text-green-600 dark:text-green-400">+{additions}</span>}
          {deletions > 0 && <span className="text-red-600 dark:text-red-400">−{deletions}</span>}
        </span>
      </div>

      {file.lines.length > 0 && (
        <div className="bg-background overflow-x-auto">
          <pre className="text-xs font-mono leading-relaxed">
            {visibleLines.map((l, i) => (
              <div key={i} className={`px-2 ${lineClass(l.kind)}`}>
                <span className="select-none opacity-60 mr-1">{linePrefix(l.kind)}</span>
                {l.text || " "}
              </div>
            ))}
          </pre>

          {hasMore && (
            <button
              onClick={() => setIsExpanded(!isExpanded)}
              className="w-full flex items-center justify-center gap-1 py-1 text-xs text-muted-foreground hover:text-foreground hover:bg-muted/40 transition-colors border-t border-border"
            >
              {isExpanded ? (
                <>
                  <ChevronUp className="h-3 w-3" />
                  Show less
                </>
              ) : (
                <>
                  <ChevronDown className="h-3 w-3" />
                  Show all ({file.lines.length} lines)
                </>
              )}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

export function ApplyPatchToolBlock({ patchText, isCompact = false }: ApplyPatchToolBlockProps) {
  const files = useMemo(() => parsePatch(patchText), [patchText])

  // Fallback: unparseable patch → raw monospace block (never markdown).
  if (!files) {
    return (
      <div className="flex items-start gap-2 text-sm bg-slate-100 dark:bg-slate-800 border border-border rounded px-3 py-2">
        <FileText className="h-4 w-4 text-muted-foreground mt-0.5 flex-shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="font-medium text-foreground/90 mb-1">
            Using tool:{" "}
            <code className="font-mono bg-muted px-1.5 py-0.5 rounded text-xs">apply_patch</code>
          </div>
          <pre className="text-xs font-mono whitespace-pre-wrap break-words text-foreground/70 overflow-x-auto">
            {patchText}
          </pre>
        </div>
      </div>
    )
  }

  if (isCompact) {
    return (
      <div className="flex flex-col gap-1 mb-1">
        {files.map((file, i) => {
          const { Icon, label } = OP_META[file.op]
          const fileName = file.path.split("/").pop() || file.path
          return (
            <div key={i} className="inline-flex items-center gap-2 text-sm text-muted-foreground/80">
              <Icon className="h-3.5 w-3.5 flex-shrink-0" />
              <span>
                {label}{" "}
                <code className="font-mono bg-muted px-1 py-0.5 rounded text-xs">{fileName}</code>
              </span>
            </div>
          )
        })}
      </div>
    )
  }

  return (
    <div className="flex items-start gap-2 text-sm bg-slate-100 dark:bg-slate-800 border border-border rounded px-3 py-2">
      <FileEdit className="h-4 w-4 text-muted-foreground mt-0.5 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <div className="text-foreground/90 mb-2">
          Applying patch{files.length > 1 ? ` (${files.length} files)` : ""}
        </div>
        <div className="space-y-2">
          {files.map((file, i) => (
            <FileDiff key={i} file={file} />
          ))}
        </div>
      </div>
    </div>
  )
}
