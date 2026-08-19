import { useEffect, useRef, useState } from "react"
import { Download, Loader2, FileWarning } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { CSVViewer } from "@/components/Environment/CSVViewer"
import { MarkdownViewer } from "@/components/Environment/MarkdownViewer"
import { JSONViewer } from "@/components/Environment/JSONViewer"
import { TextViewer } from "@/components/Environment/TextViewer"
import { fetchAuthenticatedBlob, saveBlobAs } from "@/utils"

interface AttachmentPreviewModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  fileId: string
  filename: string
  mimeType: string
  size?: number
}

type PreviewKind = "image" | "pdf" | "csv" | "markdown" | "json" | "text" | "none"

function classifyMime(mimeType: string): PreviewKind {
  const mime = (mimeType || "").toLowerCase()
  if (mime.startsWith("image/")) return "image"
  if (mime === "application/pdf") return "pdf"
  if (mime === "text/csv") return "csv"
  if (mime === "text/markdown" || mime === "text/x-markdown") return "markdown"
  if (mime === "application/json") return "json"
  if (mime.startsWith("text/")) return "text"
  return "none"
}

function formatFileSize(bytes?: number): string {
  if (bytes == null) return ""
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Authenticated blob fetch against the files download endpoint. */
async function fetchFileBlob(
  fileId: string,
  { inline }: { inline: boolean }
): Promise<Blob> {
  const dispositionParam = inline ? "?disposition=inline" : ""
  return fetchAuthenticatedBlob(
    `/api/v1/files/${fileId}/download${dispositionParam}`
  )
}

async function downloadFile(fileId: string, filename: string): Promise<void> {
  saveBlobAs(await fetchFileBlob(fileId, { inline: false }), filename)
}

export function AttachmentPreviewModal({
  open,
  onOpenChange,
  fileId,
  filename,
  mimeType,
  size,
}: AttachmentPreviewModalProps) {
  const kind = classifyMime(mimeType)
  const usesObjectUrl = kind === "image" || kind === "pdf"
  const usesTextContent =
    kind === "csv" || kind === "markdown" || kind === "json" || kind === "text"

  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  const [textContent, setTextContent] = useState<string | null>(null)
  const objectUrlRef = useRef<string | null>(null)

  const revokeObjectUrl = () => {
    if (objectUrlRef.current) {
      window.URL.revokeObjectURL(objectUrlRef.current)
      objectUrlRef.current = null
    }
  }

  // Fetch preview content when the modal opens (object URL for binary previews,
  // text content for the text-based viewers). Revoke object URLs on close.
  useEffect(() => {
    if (!open) {
      revokeObjectUrl()
      setObjectUrl(null)
      setTextContent(null)
      setError(null)
      setIsLoading(false)
      return
    }

    if (kind === "none") return

    let cancelled = false
    setIsLoading(true)
    setError(null)

    fetchFileBlob(fileId, { inline: true })
      .then(async (blob) => {
        if (cancelled) return
        if (usesObjectUrl) {
          const url = window.URL.createObjectURL(blob)
          objectUrlRef.current = url
          setObjectUrl(url)
        } else if (usesTextContent) {
          const text = await blob.text()
          if (cancelled) return
          setTextContent(text)
        }
        setIsLoading(false)
      })
      .catch((e) => {
        if (cancelled) return
        console.error("Failed to load attachment preview:", e)
        setError("Couldn't load preview — try downloading.")
        setIsLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [open, fileId, kind, usesObjectUrl, usesTextContent])

  // Revoke any object URL on unmount.
  useEffect(() => {
    return () => revokeObjectUrl()
  }, [])

  const handleDownload = async () => {
    try {
      await downloadFile(fileId, filename)
    } catch (e) {
      console.error("Failed to download attachment:", e)
    }
  }

  const renderBody = () => {
    if (kind === "none") {
      return (
        <div className="flex flex-col items-center justify-center gap-4 py-12 text-center">
          <FileWarning className="h-8 w-8 text-muted-foreground" />
          <p className="text-sm text-muted-foreground">
            No preview available for this file type.
          </p>
          <Button variant="outline" onClick={handleDownload}>
            <Download className="h-4 w-4 mr-2" />
            Download
          </Button>
        </div>
      )
    }

    if (isLoading) {
      return (
        <div className="flex items-center justify-center gap-2 py-16 text-muted-foreground">
          <Loader2 className="h-5 w-5 animate-spin" />
          <span className="text-sm">Loading preview…</span>
        </div>
      )
    }

    if (error) {
      return (
        <div className="flex flex-col items-center justify-center gap-4 py-12 text-center">
          <FileWarning className="h-8 w-8 text-destructive" />
          <p className="text-sm text-destructive">{error}</p>
          <Button variant="outline" onClick={handleDownload}>
            <Download className="h-4 w-4 mr-2" />
            Download
          </Button>
        </div>
      )
    }

    if (kind === "image" && objectUrl) {
      return (
        <div className="flex items-center justify-center overflow-auto max-h-[70vh] p-4">
          <img
            src={objectUrl}
            alt={filename}
            className="max-w-full max-h-[68vh] object-contain"
          />
        </div>
      )
    }

    if (kind === "pdf" && objectUrl) {
      return (
        <iframe
          src={objectUrl}
          title={filename}
          className="w-full h-[70vh] border-0"
        />
      )
    }

    if (textContent != null) {
      return (
        <div className="h-[70vh] overflow-hidden">
          {kind === "csv" && <CSVViewer content={textContent} />}
          {kind === "markdown" && <MarkdownViewer content={textContent} />}
          {kind === "json" && <JSONViewer content={textContent} />}
          {kind === "text" && <TextViewer content={textContent} />}
        </div>
      )
    }

    return null
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl p-0 gap-0">
        <DialogHeader className="px-6 py-4 border-b">
          <div className="flex items-center justify-between gap-4 pr-6">
            <div className="min-w-0">
              <DialogTitle className="truncate text-base">{filename}</DialogTitle>
              {size != null && (
                <p className="text-xs text-muted-foreground">
                  {formatFileSize(size)}
                </p>
              )}
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={handleDownload}
              className="shrink-0"
            >
              <Download className="h-4 w-4 mr-2" />
              Download
            </Button>
          </div>
        </DialogHeader>
        <div className="overflow-hidden">{renderBody()}</div>
      </DialogContent>
    </Dialog>
  )
}
