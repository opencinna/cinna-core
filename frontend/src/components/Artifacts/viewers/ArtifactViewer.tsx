import { AppWindow, Download } from "lucide-react"
import { useMemo } from "react"

import type { ArtifactFileEntry, ArtifactViewPublic } from "@/client"
import { Button } from "@/components/ui/button"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { formatBytes } from "../artifactFormat"
import { ArtifactFileTree } from "./ArtifactFileTree"
import { FilePreview } from "./FilePreview"
import { defaultFilePath, downloadUrl, fileUrl, previewKind } from "./fileKinds"
import { SandboxedFrame } from "./SimpleFileViewers"
import { ViewerMessage, ViewerSkeleton } from "./ViewerStates"

interface OpenAsSite {
  /** Owner only: PATCH `entry_path` to this `.html` file. */
  onOpenAsSite: (path: string) => void
  pending: boolean
}

function FileBar({
  file,
  contentBase,
  openAsSite,
  picker,
}: {
  file: ArtifactFileEntry
  contentBase: string
  openAsSite?: OpenAsSite
  /** Below `sm` the tree is hidden; the path becomes a picker over the files. */
  picker?: { files: ArtifactFileEntry[]; onSelect: (path: string) => void }
}) {
  return (
    <div className="flex h-10 shrink-0 items-center gap-2 border-b px-3">
      {picker && (
        <Select value={file.path} onValueChange={picker.onSelect}>
          <SelectTrigger
            size="sm"
            className="h-8 min-w-0 flex-1 font-mono text-xs sm:hidden"
            aria-label="File"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {picker.files.map((f) => (
              <SelectItem
                key={f.path}
                value={f.path}
                className="font-mono text-xs"
              >
                {f.path}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className={cn(
              "min-w-0 truncate font-mono text-xs",
              picker && "hidden sm:inline",
            )}
          >
            {file.path}
          </span>
        </TooltipTrigger>
        <TooltipContent side="bottom" className="max-w-md break-all text-xs">
          {file.path}
        </TooltipContent>
      </Tooltip>
      <span className="shrink-0 text-xs text-muted-foreground">
        {formatBytes(file.size)}
      </span>
      <div className="ml-auto flex shrink-0 items-center gap-1">
        {openAsSite && previewKind(file) === "html" && (
          <LoadingButton
            variant="outline"
            size="sm"
            loading={openAsSite.pending}
            onClick={() => openAsSite.onOpenAsSite(file.path)}
          >
            <AppWindow className="h-4 w-4" />
            Open as site
          </LoadingButton>
        )}
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon" className="h-7 w-7" asChild>
              <a
                href={downloadUrl(contentBase, file.path)}
                aria-label="Download file"
              >
                <Download className="h-3.5 w-3.5" />
              </a>
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom" className="text-xs">
            Download file
          </TooltipContent>
        </Tooltip>
      </div>
    </div>
  )
}

/** The file the pane shows: the requested one when it exists, else the default. */
export function resolveSelectedFile(
  files: ArtifactFileEntry[],
  requested: string | undefined,
): ArtifactFileEntry | null {
  const path =
    requested && files.some((f) => f.path === requested)
      ? requested
      : defaultFilePath(files)
  return files.find((f) => f.path === path) ?? null
}

/** Where "Open full screen" goes: the site's entry, else the selected file. */
export function fullScreenUrl(
  view: ArtifactViewPublic,
  requested: string | undefined,
): string | null {
  if (view.entry) return fileUrl(view.content_base, view.entry)
  const file = resolveSelectedFile(view.files, requested)
  return file ? fileUrl(view.content_base, file.path) : null
}

interface ArtifactViewerProps {
  title: string
  view: ArtifactViewPublic | undefined
  isError: boolean
  onRetry: () => void
  selectedFile: string | undefined
  onSelectFile: (path: string) => void
  openAsSite?: OpenAsSite
}

/**
 * S5 dispatcher: a site is one sandboxed iframe; a set of files is the files
 * browser (tree + file bar + the file's viewer); one file skips the tree.
 */
export function ArtifactViewer({
  title,
  view,
  isError,
  onRetry,
  selectedFile,
  onSelectFile,
  openAsSite,
}: ArtifactViewerProps) {
  const paths = useMemo(
    () => new Set((view?.files ?? []).map((f) => f.path)),
    [view?.files],
  )

  if (isError && !view) {
    return (
      <ViewerMessage
        action={
          <Button variant="outline" size="sm" onClick={onRetry}>
            Retry
          </Button>
        }
      >
        Couldn't load {title}.
      </ViewerMessage>
    )
  }
  if (!view) return <ViewerSkeleton />

  if (view.entry) {
    return (
      <SandboxedFrame
        src={fileUrl(view.content_base, view.entry)}
        title={title}
      />
    )
  }

  if (view.files.length === 0) {
    return <ViewerMessage>This revision has no files.</ViewerMessage>
  }

  const file = resolveSelectedFile(view.files, selectedFile)
  const pane = file && (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <FileBar
        file={file}
        contentBase={view.content_base}
        openAsSite={openAsSite}
        picker={
          view.files.length > 1
            ? { files: view.files, onSelect: onSelectFile }
            : undefined
        }
      />
      <div className="min-h-0 flex-1 overflow-hidden">
        <FilePreview
          // A new file is a new viewer: no scroll or zoom carried over.
          key={file.path}
          contentBase={view.content_base}
          file={file}
          paths={paths}
          onSelectFile={onSelectFile}
        />
      </div>
    </div>
  )

  if (view.files.length === 1) {
    return <div className="flex h-full min-h-0">{pane}</div>
  }

  return (
    <div className="flex h-full min-h-0">
      <div className="hidden w-60 shrink-0 border-r sm:block">
        <ArtifactFileTree
          files={view.files}
          selected={file?.path ?? null}
          onSelect={onSelectFile}
        />
      </div>
      {pane}
    </div>
  )
}
