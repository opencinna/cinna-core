import { FileArchive, Files, X } from "lucide-react"
import { type ChangeEvent, useRef } from "react"
import { useDropzone } from "react-dropzone"

import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { formatBytes, plural } from "./artifactFormat"

/** What the user picked, normalised to the D14 upload shape. */
export type UploadSource =
  | { kind: "archive"; file: File; size: number; suggestedTitle: string }
  | {
      kind: "files"
      /** Each file renamed to its path inside the artifact. */
      files: File[]
      size: number
      suggestedTitle: string
    }

function stripExtension(name: string): string {
  const dot = name.lastIndexOf(".")
  return dot > 0 ? name.slice(0, dot) : name
}

/** A picked or dropped file's path relative to what was picked. */
function relativePath(file: File): string {
  const withPath = file as File & { path?: string; relativePath?: string }
  const raw =
    file.webkitRelativePath ||
    withPath.relativePath ||
    withPath.path ||
    file.name
  return raw.replace(/^\.?\/+/, "")
}

/**
 * Normalise a pick: a lone `.zip` is sent as the archive; otherwise every file
 * is sent under its relative path. A folder pick (every path under one top
 * segment) drops that segment, so its `index.html` lands at the root.
 */
export function toUploadSource(picked: File[]): UploadSource | null {
  if (picked.length === 0) return null
  if (picked.length === 1 && /\.zip$/i.test(picked[0].name)) {
    const file = picked[0]
    return {
      kind: "archive",
      file,
      size: file.size,
      suggestedTitle: stripExtension(file.name),
    }
  }
  const paths = picked.map(relativePath)
  const tops = new Set(
    paths.map((p) => (p.includes("/") ? p.split("/")[0] : "")),
  )
  const folder = tops.size === 1 && !tops.has("") ? [...tops][0] : null
  const files = picked.map((file, i) => {
    const path = folder ? paths[i].slice(folder.length + 1) : paths[i]
    return new File([file], path, { type: file.type })
  })
  return {
    kind: "files",
    files,
    size: picked.reduce((sum, f) => sum + f.size, 0),
    suggestedTitle:
      folder ?? (picked.length === 1 ? stripExtension(picked[0].name) : ""),
  }
}

export function uploadSourceSummary(source: UploadSource): string {
  return source.kind === "archive"
    ? `${source.file.name} · ${formatBytes(source.size)}`
    : `${plural(source.files.length, "file")} · ${formatBytes(source.size)}`
}

interface UploadSourcePickerProps {
  source: UploadSource | null
  onChange: (source: UploadSource | null) => void
  disabled?: boolean
}

/**
 * S3 block 1 — a dashed drop zone with "Choose files" and "Choose folder";
 * once something is picked, one summary line with a Clear button instead.
 */
export function UploadSourcePicker({
  source,
  onChange,
  disabled,
}: UploadSourcePickerProps) {
  const filesInput = useRef<HTMLInputElement>(null)
  const folderInput = useRef<HTMLInputElement>(null)
  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    noClick: true,
    noKeyboard: true,
    disabled,
    onDrop: (accepted) => onChange(toUploadSource(accepted)),
  })

  const fromInput = (e: ChangeEvent<HTMLInputElement>) => {
    onChange(toUploadSource(Array.from(e.target.files ?? [])))
    e.target.value = ""
  }

  if (source) {
    const Icon = source.kind === "archive" ? FileArchive : Files
    return (
      <div className="flex items-center gap-2 rounded-md border px-3 py-2">
        <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate text-sm">
          {uploadSourceSummary(source)}
        </span>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              aria-label="Clear"
              disabled={disabled}
              onClick={() => onChange(null)}
            >
              <X className="h-3.5 w-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top" className="text-xs">
            Clear
          </TooltipContent>
        </Tooltip>
      </div>
    )
  }

  return (
    <div
      {...getRootProps()}
      className={cn(
        "rounded-md border border-dashed p-4 text-center text-sm",
        isDragActive && "border-primary bg-primary/5",
      )}
    >
      <input {...getInputProps()} />
      <p className="text-muted-foreground">
        Drop a zip, a folder or files here
      </p>
      <div className="mt-1 flex items-center justify-center gap-1">
        <Button
          type="button"
          variant="link"
          size="sm"
          disabled={disabled}
          onClick={() => filesInput.current?.click()}
        >
          Choose files
        </Button>
        <span className="text-muted-foreground" aria-hidden="true">
          ·
        </span>
        <Button
          type="button"
          variant="link"
          size="sm"
          disabled={disabled}
          onClick={() => folderInput.current?.click()}
        >
          Choose folder
        </Button>
      </div>
      <input
        ref={filesInput}
        type="file"
        multiple
        className="hidden"
        onChange={fromInput}
        data-testid="artifact-upload-files"
      />
      <input
        ref={folderInput}
        type="file"
        className="hidden"
        onChange={fromInput}
        // React does not type the directory picker attribute.
        {...{ webkitdirectory: "" }}
      />
    </div>
  )
}
