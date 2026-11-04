import { useQuery } from "@tanstack/react-query"
import { useEffect, useRef, useState } from "react"
import type { DashboardTileDataPublic } from "@/client"

import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { useTheme } from "@/components/theme-provider"
import { Skeleton } from "@/components/ui/skeleton"
import { type BridgeInit, FrameBridge, type HostContext } from "./bridgeHost"
import type { PageContext } from "./dashboardFocus"
import { componentErrorMessage, readComponentError } from "./tileConfig"
import type { TileController } from "./tileController"

/** The app's tokens a page can follow, as `--cinna-*` (kit template `style.css`). */
const VARIABLES: Record<string, string> = {
  "--cinna-bg": "--background",
  "--cinna-fg": "--foreground",
  "--cinna-muted": "--muted-foreground",
  "--cinna-border": "--border",
  "--cinna-accent": "--primary",
}

function readHostContext(theme: "light" | "dark"): HostContext {
  const style = getComputedStyle(document.documentElement)
  const variables: Record<string, string> = {}
  for (const [name, token] of Object.entries(VARIABLES)) {
    const value = style.getPropertyValue(token).trim()
    if (value) variables[name] = value
  }
  const font = getComputedStyle(document.body).fontFamily
  if (font) variables["--cinna-font"] = font
  return { theme, styles: { variables } }
}

/** Stale while a refresh runs, and still stale when the last one failed. */
function asSent(
  data: DashboardTileDataPublic,
  isRefreshing: boolean,
  runIssue: string | null,
): DashboardTileDataPublic {
  return isRefreshing || runIssue ? { ...data, stale: true } : data
}

/** The §3 code a page sees; environment trouble is `call_failed` (plan §13 §3). */
function toolErrorCode(error: unknown): string {
  const code = readComponentError(error)?.code ?? "call_failed"
  return code === "env_unavailable" || code === "adapter_unsupported"
    ? "call_failed"
    : code
}

interface Props {
  blockId: string
  frameQueryKey: readonly unknown[]
  controller: TileController
  init: BridgeInit
  onFrameFocus: () => void
  onModelContext: (context: PageContext) => void
  onMessage: (text: string) => boolean
  confirmLink: (url: string) => Promise<boolean> | null
}

/**
 * `view: "html"` — the agent's page in a sandboxed frame (no
 * `allow-same-origin`, no popups, no top navigation; the content host sends
 * the same sandbox as a CSP directive), wired to `FrameBridge`. A `Skeleton`
 * covers it until the page's `ui/notifications/initialized`.
 */
export function HtmlFrameView({
  blockId,
  frameQueryKey,
  controller,
  init,
  onFrameFocus,
  onModelContext,
  onMessage,
  confirmLink,
}: Props) {
  const { resolvedTheme } = useTheme()
  const iframeRef = useRef<HTMLIFrameElement | null>(null)
  const [initialized, setInitialized] = useState(false)

  const frameQuery = useQuery({
    queryKey: frameQueryKey,
    queryFn: controller.mintFrame,
    // The cap lasts 12 h and the loaded page keeps working past it; a new URL
    // would reload the page, so this frame keeps the one it opened with.
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: 0,
    retry: false,
  })

  // The bridge reads everything through refs, so it is created once per frame
  // and never torn down by a re-render (a reset there would drop the nonce).
  const latest = useRef({
    controller,
    init,
    onModelContext,
    onMessage,
    confirmLink,
    resolvedTheme,
  })
  latest.current = {
    controller,
    init,
    onModelContext,
    onMessage,
    confirmLink,
    resolvedTheme,
  }
  const bridgeRef = useRef<FrameBridge | null>(null)
  const frameUrl = frameQuery.data?.frame_url

  useEffect(() => {
    const iframe = iframeRef.current
    if (!frameUrl || !iframe) return
    const bridge = new FrameBridge(blockId, {
      init: () => latest.current.init,
      hostContext: () => readHostContext(latest.current.resolvedTheme),
      storedParams: () => latest.current.controller.storedParams,
      setParams: (params) => latest.current.controller.setParams(params),
      callEndpoint: (name, args) =>
        latest.current.controller.callEndpoint(name, args),
      callError: (error) => ({
        code: toolErrorCode(error),
        message: componentErrorMessage(error, "The endpoint failed."),
      }),
      setPageState: (state) => latest.current.controller.setPageState(state),
      onModelContext: (context) => latest.current.onModelContext(context),
      onMessage: (text) => latest.current.onMessage(text),
      confirmLink: (url) => latest.current.confirmLink(url),
      onInitialized: () => setInitialized(true),
    })
    bridge.attach(iframe)
    bridgeRef.current = bridge
    // The tile usually has its data before the frame URL: hand it over now,
    // or the page waits for a data change that may never come.
    const seed = latest.current.controller
    if (seed.data)
      bridge.pushData(asSent(seed.data, seed.isRefreshing, seed.runIssue))
    return () => {
      bridge.detach()
      bridgeRef.current = null
    }
  }, [blockId, frameUrl])

  const data = controller.data
  const isRefreshing = controller.isRefreshing
  const runIssue = controller.runIssue
  useEffect(() => {
    // Cached first (stale while a refresh runs), then the fresh result.
    if (data) bridgeRef.current?.pushData(asSent(data, isRefreshing, runIssue))
  }, [data, isRefreshing, runIssue])

  useEffect(() => {
    bridgeRef.current?.pushHostContext(readHostContext(resolvedTheme))
  }, [resolvedTheme])

  // The page taking focus: the window blurs while the frame is the active element.
  useEffect(() => {
    const onBlur = () => {
      setTimeout(() => {
        if (document.activeElement === iframeRef.current) onFrameFocus()
      }, 0)
    }
    window.addEventListener("blur", onBlur)
    return () => window.removeEventListener("blur", onBlur)
  }, [onFrameFocus])

  if (frameQuery.isError) {
    return (
      <div className="p-2">
        <QueryErrorAlert
          compact
          error={{
            message: componentErrorMessage(
              frameQuery.error,
              "The page could not be opened.",
            ),
          }}
          fallback="Couldn't load this tile."
          onRetry={() => void frameQuery.refetch()}
        />
      </div>
    )
  }

  return (
    <div className="relative h-full w-full">
      {frameUrl && (
        <iframe
          ref={iframeRef}
          src={frameUrl}
          title={init.title}
          sandbox="allow-scripts allow-forms allow-modals"
          referrerPolicy="no-referrer"
          className="h-full w-full border-0"
        />
      )}
      {!initialized && <Skeleton className="absolute inset-0 rounded-none" />}
    </div>
  )
}
