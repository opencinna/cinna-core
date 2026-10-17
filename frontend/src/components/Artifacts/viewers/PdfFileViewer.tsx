import { Minus, Plus } from "lucide-react"
import type { PDFDocumentProxy } from "pdfjs-dist"
import { useEffect, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { ViewerError, ViewerSkeleton } from "./ViewerStates"

const ZOOM_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 2, 3]
const PAGE_GAP = 16
const SIDE_PADDING = 32

/**
 * pdf.js, loaded on first use: it is the one large dependency of the viewer.
 * The worker is bundled by Vite as a classic `.js` worker — a `.mjs` asset is
 * served as `application/octet-stream` by the nginx image and refused.
 */
async function loadPdfJs() {
  const [pdfjs, { default: PdfWorker }] = await Promise.all([
    import("pdfjs-dist"),
    import("pdfjs-dist/build/pdf.worker.min.mjs?worker"),
  ])
  if (!pdfjs.GlobalWorkerOptions.workerPort) {
    pdfjs.GlobalWorkerOptions.workerPort = new PdfWorker()
  }
  return pdfjs
}

function PdfPage({
  doc,
  number,
  width,
  aspect,
  onVisible,
}: {
  doc: PDFDocumentProxy
  number: number
  width: number
  /** height / width of the first page, for the placeholder before render. */
  aspect: number
  onVisible: (number: number, ratio: number) => void
}) {
  const holderRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [near, setNear] = useState(false)
  const [height, setHeight] = useState(width * aspect)
  const [failed, setFailed] = useState(false)

  // Rendered only once it comes near the viewport.
  useEffect(() => {
    const el = holderRef.current
    if (!el) return
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) setNear(true)
          onVisible(number, entry.intersectionRatio)
        }
      },
      { rootMargin: "600px 0px", threshold: [0, 0.25, 0.5, 0.75, 1] },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [number, onVisible])

  useEffect(() => {
    if (!near) return
    let cancelled = false
    let task: { cancel: () => void; promise: Promise<void> } | null = null
    setFailed(false)
    void (async () => {
      try {
        const page = await doc.getPage(number)
        if (cancelled) return
        const base = page.getViewport({ scale: 1 })
        const viewport = page.getViewport({ scale: width / base.width })
        const canvas = canvasRef.current
        if (!canvas) return
        const ratio = window.devicePixelRatio || 1
        canvas.width = Math.floor(viewport.width * ratio)
        canvas.height = Math.floor(viewport.height * ratio)
        canvas.style.width = `${viewport.width}px`
        canvas.style.height = `${viewport.height}px`
        setHeight(viewport.height)
        const context = canvas.getContext("2d")
        if (!context) throw new Error("no 2d context")
        task = page.render({
          canvasContext: context,
          viewport,
          transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
        })
        await task.promise
      } catch (error) {
        // A re-render (zoom, unmount) cancels the previous task: not a failure.
        const cancelledRender =
          error instanceof Error && error.name === "RenderingCancelledException"
        if (!cancelled && !cancelledRender) setFailed(true)
      }
    })()
    return () => {
      cancelled = true
      task?.cancel()
    }
  }, [doc, near, number, width])

  return (
    <div
      ref={holderRef}
      className="mx-auto bg-white shadow-sm"
      style={{ width, height }}
      data-page={number}
    >
      {failed && (
        <div className="flex h-full items-center justify-center p-4 text-center text-sm text-muted-foreground">
          Couldn't render page {number}.
        </div>
      )}
      <canvas
        ref={canvasRef}
        aria-label={`Page ${number}`}
        className={failed ? "hidden" : undefined}
      />
    </div>
  )
}

/**
 * PDF rendered in the page with pdf.js: pages stacked, fit to the pane's
 * width, each drawn when it nears the viewport; a floating bar with the
 * page counter and zoom.
 */
export function PdfFileViewer({ url, name }: { url: string; name: string }) {
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null)
  const [aspect, setAspect] = useState(1.294)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [zoomIndex, setZoomIndex] = useState(ZOOM_STEPS.indexOf(1))
  const [paneWidth, setPaneWidth] = useState(0)
  const [currentPage, setCurrentPage] = useState(1)
  const scrollRef = useRef<HTMLDivElement>(null)
  const visibility = useRef(new Map<number, number>())

  // biome-ignore lint/correctness/useExhaustiveDependencies: `attempt` is the Retry trigger
  useEffect(() => {
    let cancelled = false
    let task: { destroy: () => Promise<void> } | null = null
    setDoc(null)
    setFailed(false)
    void (async () => {
      try {
        const pdfjs = await loadPdfJs()
        const loadingTask = pdfjs.getDocument({
          url,
          isEvalSupported: false,
          enableXfa: false,
        })
        task = loadingTask
        const loaded = await loadingTask.promise
        const first = await loaded.getPage(1)
        const viewport = first.getViewport({ scale: 1 })
        if (cancelled) return
        setAspect(viewport.height / viewport.width)
        setDoc(loaded)
      } catch {
        if (!cancelled) setFailed(true)
      }
    })()
    return () => {
      cancelled = true
      void task?.destroy()
    }
  }, [url, attempt])

  // biome-ignore lint/correctness/useExhaustiveDependencies: the scroll pane mounts once `doc` loads
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const observer = new ResizeObserver(([entry]) =>
      setPaneWidth(entry.contentRect.width),
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [doc])

  const onVisible = useRef((number: number, ratio: number) => {
    visibility.current.set(number, ratio)
    let best = 1
    let bestRatio = -1
    for (const [page, r] of visibility.current) {
      if (r > bestRatio) {
        best = page
        bestRatio = r
      }
    }
    setCurrentPage(best)
  }).current

  if (failed) {
    return <ViewerError name={name} onRetry={() => setAttempt((a) => a + 1)} />
  }
  if (!doc) return <ViewerSkeleton />

  const fitWidth = Math.max(paneWidth - SIDE_PADDING, 200)
  const width = Math.round(fitWidth * ZOOM_STEPS[zoomIndex])

  return (
    <div className="relative h-full min-h-0">
      <div
        ref={scrollRef}
        className="h-full overflow-auto bg-muted/30 py-4"
        style={{ display: "grid", gap: PAGE_GAP, alignContent: "start" }}
      >
        {paneWidth > 0 &&
          Array.from({ length: doc.numPages }, (_, i) => (
            <PdfPage
              key={i + 1}
              doc={doc}
              number={i + 1}
              width={width}
              aspect={aspect}
              onVisible={onVisible}
            />
          ))}
      </div>
      <div className="absolute bottom-4 left-1/2 flex -translate-x-1/2 items-center gap-1 rounded-md border bg-background/95 px-2 py-1 shadow-sm">
        <span className="px-1 text-xs tabular-nums" aria-live="polite">
          {currentPage} / {doc.numPages}
        </span>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              aria-label="Zoom out"
              disabled={zoomIndex === 0}
              onClick={() => setZoomIndex((z) => Math.max(z - 1, 0))}
            >
              <Minus className="h-3.5 w-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top" className="text-xs">
            Zoom out
          </TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              aria-label="Zoom in"
              disabled={zoomIndex === ZOOM_STEPS.length - 1}
              onClick={() =>
                setZoomIndex((z) => Math.min(z + 1, ZOOM_STEPS.length - 1))
              }
            >
              <Plus className="h-3.5 w-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top" className="text-xs">
            Zoom in
          </TooltipContent>
        </Tooltip>
      </div>
    </div>
  )
}
