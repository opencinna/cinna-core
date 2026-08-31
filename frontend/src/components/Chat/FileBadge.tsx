import { X, File, FileText, Image, Archive } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import type { FileUploadPublic } from "@/client"
import { downloadAuthenticatedFile } from "@/utils"

interface FileBadgeProps {
  file: FileUploadPublic
  onRemove?: () => void
  downloadable?: boolean
  /**
   * When provided, clicking the badge body opens a preview (via this handler)
   * instead of forcing a download. The explicit download path stays available
   * through the badge's own download fallback when `downloadable` is set and no
   * preview handler is given.
   */
  onPreview?: (file: FileUploadPublic) => void
}

export function FileBadge({ file, onRemove, downloadable = false, onPreview }: FileBadgeProps) {
  // `source` distinguishes agent-authored attachments from user uploads.
  const isAgentAttachment = file.source === "agent_attachment"
  const getFileIcon = (mimeType: string) => {
    if (mimeType.startsWith('image/')) return <Image className="h-3 w-3" />
    if (mimeType.startsWith('text/')) return <FileText className="h-3 w-3" />
    if (mimeType.includes('zip') || mimeType.includes('tar')) return <Archive className="h-3 w-3" />
    return <File className="h-3 w-3" />
  }

  const truncateFilename = (name: string, maxLength: number = 20) => {
    if (name.length <= maxLength) return name
    const ext = name.split('.').pop()
    const base = name.substring(0, maxLength - (ext?.length || 0) - 4)
    return `${base}...${ext}`
  }

  const formatFileSize = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  const handleClick = async () => {
    if (onPreview) {
      onPreview(file)
      return
    }
    if (downloadable) {
      try {
        await downloadAuthenticatedFile(
          `/api/v1/files/${file.id}/download`,
          file.filename,
        )
      } catch (error) {
        console.error('Failed to download file:', error)
      }
    }
  }

  const isClickable = onPreview != null || downloadable

  return (
    <Badge
      variant="secondary"
      className={`flex items-center gap-1 pl-2 pr-1 ${isClickable ? 'cursor-pointer hover:bg-secondary/80' : ''} ${isAgentAttachment ? 'border border-border' : ''}`}
      onClick={isClickable ? handleClick : undefined}
      title={`${file.filename} (${formatFileSize(file.file_size)})${onPreview ? ' — preview' : ''}`}
    >
      {getFileIcon(file.mime_type)}
      <span className="text-xs">{truncateFilename(file.filename)}</span>
      {onRemove && (
        <button
          onClick={(e) => {
            e.stopPropagation()
            onRemove()
          }}
          className="ml-1 hover:bg-background/50 rounded-full p-0.5"
          aria-label="Remove file"
        >
          <X className="h-3 w-3" />
        </button>
      )}
    </Badge>
  )
}
