import { useState } from "react"
import {
  Download,
  File,
  FileText,
  Image,
  Archive,
  Paperclip,
  AlertTriangle,
} from "lucide-react"
import { AttachmentPreviewModal } from "./AttachmentPreviewModal"
import { downloadAuthenticatedFile } from "@/utils"

interface AttachmentBlockProps {
  /** Event variant: a successful attachment card, or a delivery-failure notice. */
  variant?: "attachment" | "attachment_error"
  fileId?: string
  filename?: string
  mimeType?: string
  size?: number
  /** Failure reason for the `attachment_error` variant. */
  errorReason?: string
  isCompact?: boolean
}

function getFileIcon(mimeType: string, className: string) {
  const mime = (mimeType || "").toLowerCase()
  if (mime.startsWith("image/")) return <Image className={className} />
  if (mime.startsWith("text/")) return <FileText className={className} />
  if (mime.includes("zip") || mime.includes("tar"))
    return <Archive className={className} />
  return <File className={className} />
}

function truncateFilename(name: string, maxLength = 28): string {
  if (name.length <= maxLength) return name
  const ext = name.split(".").pop()
  const base = name.substring(0, maxLength - (ext?.length || 0) - 4)
  return `${base}...${ext}`
}

function formatFileSize(bytes?: number): string {
  if (bytes == null) return ""
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

async function downloadFile(fileId: string, filename: string): Promise<void> {
  await downloadAuthenticatedFile(`/api/v1/files/${fileId}/download`, filename)
}

export function AttachmentBlock({
  variant = "attachment",
  fileId,
  filename,
  mimeType = "",
  size,
  errorReason,
  isCompact,
}: AttachmentBlockProps) {
  const [showPreview, setShowPreview] = useState(false)

  // ── Error notice ─────────────────────────────────────────────────────────
  if (variant === "attachment_error") {
    return (
      <div className="flex items-start gap-2 text-sm px-3 py-2 rounded border-l-4 border-amber-500 bg-amber-50/70 dark:bg-amber-950/30 text-amber-900 dark:text-amber-200">
        <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
        <span>{errorReason || "An attachment could not be delivered."}</span>
      </div>
    )
  }

  // Without a file id there is nothing actionable to render.
  if (!fileId) return null

  const displayName = filename || "attachment"

  const handleDownload = async (e: React.MouseEvent) => {
    e.stopPropagation()
    try {
      await downloadFile(fileId, displayName)
    } catch (err) {
      console.error("Failed to download attachment:", err)
    }
  }

  const openPreview = () => setShowPreview(true)

  // ── Compact one-liner ──────────────────────────────────────────────────────
  if (isCompact) {
    return (
      <>
        <button
          type="button"
          onClick={openPreview}
          aria-label={`Attachment: ${displayName} (${formatFileSize(size)})`}
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground/90 hover:text-foreground transition-colors mb-1"
        >
          <Paperclip className="h-3.5 w-3.5 flex-shrink-0" />
          <span className="underline underline-offset-2 truncate max-w-[200px]">
            {truncateFilename(displayName)}
          </span>
        </button>
        <AttachmentPreviewModal
          open={showPreview}
          onOpenChange={setShowPreview}
          fileId={fileId}
          filename={displayName}
          mimeType={mimeType}
          size={size}
        />
      </>
    )
  }

  // ── Full card ──────────────────────────────────────────────────────────────
  return (
    <>
      <div
        role="button"
        tabIndex={0}
        aria-label={`Attachment: ${displayName} (${formatFileSize(size)}) — open preview`}
        onClick={openPreview}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault()
            openPreview()
          }
        }}
        className="group flex items-center gap-3 text-sm bg-slate-100 dark:bg-slate-800 border border-border rounded px-3 py-2 cursor-pointer hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors max-w-sm"
      >
        <span className="text-muted-foreground shrink-0">
          {getFileIcon(mimeType, "h-5 w-5")}
        </span>
        <div className="flex-1 min-w-0">
          <p className="font-medium text-foreground/90 truncate" title={displayName}>
            {truncateFilename(displayName)}
          </p>
          {size != null && (
            <p className="text-xs text-muted-foreground">{formatFileSize(size)}</p>
          )}
        </div>
        <button
          type="button"
          onClick={handleDownload}
          aria-label={`Download ${displayName}`}
          className="shrink-0 p-1.5 rounded hover:bg-background/70 text-muted-foreground hover:text-foreground transition-colors"
          title="Download"
        >
          <Download className="h-4 w-4" />
        </button>
      </div>
      <AttachmentPreviewModal
        open={showPreview}
        onOpenChange={setShowPreview}
        fileId={fileId}
        filename={displayName}
        mimeType={mimeType}
        size={size}
      />
    </>
  )
}
