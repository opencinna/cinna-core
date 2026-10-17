import { Download } from "lucide-react"

import { FileIcon } from "@/components/Environment/FileIcon"
import { Button } from "@/components/ui/button"
import { formatBytes } from "../artifactFormat"
import { extensionOf } from "./fileKinds"

/** An image (SVG included) as an `<img>`: it cannot run script that way. */
export function ImageFileViewer({ url, name }: { url: string; name: string }) {
  return (
    <div className="flex h-full items-center justify-center overflow-auto bg-muted/30 p-4">
      <img
        src={url}
        alt={name}
        className="max-h-full max-w-full object-contain"
      />
    </div>
  )
}

/**
 * HTML as a site: an iframe on the content origin with the sandbox of plan §4.
 * Never `allow-same-origin` — that would hand the page this app's origin.
 */
export function SandboxedFrame({ src, title }: { src: string; title: string }) {
  return (
    <iframe
      src={src}
      title={title}
      sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals allow-forms"
      className="h-full w-full border-0 bg-white"
      referrerPolicy="no-referrer"
    />
  )
}

/** A file with no preview, or one too large to preview. */
export function DownloadOnlyViewer({
  name,
  path,
  size,
  href,
  reason,
}: {
  name: string
  path: string
  size: number
  href: string
  /** Why there is no preview, when it is not just the type. */
  reason?: string
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
      <FileIcon fileType={extensionOf(path)} className="h-10 w-10" />
      <div>
        <p className="text-sm font-medium break-all">{name}</p>
        <p className="text-xs text-muted-foreground">{formatBytes(size)}</p>
      </div>
      {reason && (
        <p className="max-w-md text-sm text-muted-foreground">{reason}</p>
      )}
      <Button variant="outline" asChild>
        <a href={href}>
          <Download className="h-4 w-4" />
          Download
        </a>
      </Button>
    </div>
  )
}
