/**
 * EnvironmentConsoleDrawer — full-height right `Sheet` hosting an xterm console.
 *
 * Two modes selected by the `kind` prop:
 *   * `terminal` — interactive PTY. Keystrokes (binary) go up, resize (text
 *     control) on fit, screen bytes come down. Owner + developer/superuser
 *     only (enforced server-side; the card already gates the button).
 *   * `logs` — write-only follow. Seeds an instant first paint from the REST
 *     `GET /environments/{id}/logs` snapshot, then streams live lines over WS.
 *
 * The drawer owns the connection status banner, the terminal sensitivity
 * notice, an autoscroll-lock "Jump to bottom" affordance for logs, and the
 * Reconnect / Close controls. The socket itself lives in
 * {@link useEnvConsoleSocket}; the renderer in {@link XtermConsole}.
 *
 * Rebuild caveat: the env-core `/shell/pty` endpoint only ships on environment
 * **rebuild**, so a pre-rebuild env may reject the terminal (the socket closes
 * and the banner offers Reconnect). Logs always work (host-side, no env-core
 * change). We don't try to detect rebuild state precisely — the close banner
 * surfaces the failure and points the user at Rebuild.
 */
import { useCallback, useEffect, useRef, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  AlertTriangle,
  ArrowDownToLine,
  RotateCw,
  ShieldAlert,
  X,
} from "lucide-react"

import { EnvironmentsService } from "@/client"
import type { AgentEnvironmentPublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { cn } from "@/lib/utils"
import {
  useEnvConsoleSocket,
  type EnvConsoleCloseReason,
  type EnvConsoleKind,
  type EnvConsoleStatus,
} from "@/hooks/useEnvConsoleSocket"
import { EnvironmentStatusBadge } from "./EnvironmentStatusBadge"
import { XtermConsole, type XtermConsoleHandle } from "./XtermConsole"

const LOGS_TAIL = 500

interface EnvironmentConsoleDrawerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  environment: AgentEnvironmentPublic
  kind: EnvConsoleKind
}

/** UTF-8 decoder shared across messages (streaming for split multi-byte runes). */
function makeDecoder(): TextDecoder {
  return new TextDecoder("utf-8")
}

const encoder = new TextEncoder()

/** Status-dot color + label for the connection indicator. */
function statusMeta(status: EnvConsoleStatus): { dot: string; label: string } {
  switch (status) {
    case "open":
      return { dot: "bg-green-500", label: "Connected" }
    case "connecting":
      return { dot: "bg-amber-500", label: "Connecting…" }
    case "error":
      return { dot: "bg-red-500", label: "Error" }
    case "closed":
    default:
      return { dot: "bg-red-500", label: "Disconnected" }
  }
}

/** Human-readable banner copy for a known close reason. */
function closeReasonCopy(
  reason: EnvConsoleCloseReason,
  kind: EnvConsoleKind,
): string | null {
  switch (reason) {
    case "env_not_running":
      return "The environment is not running. Start it, then reconnect."
    case "concurrency_exceeded":
      return "Too many consoles are open for this environment. Close one and try again."
    case "unauthorized":
      return "You are not allowed to open this console."
    case "idle_timeout":
      return "The terminal closed after a period of inactivity."
    case "container_stopped":
      return "The container stopped. Logs ended — reconnect once it is running again."
    case "error":
      return kind === "terminal"
        ? "The terminal connection dropped. If this environment hasn't been rebuilt recently, rebuild it to enable the terminal."
        : "The logs connection dropped."
    default:
      return null
  }
}

export function EnvironmentConsoleDrawer({
  open,
  onOpenChange,
  environment,
  kind,
}: EnvironmentConsoleDrawerProps) {
  const consoleRef = useRef<XtermConsoleHandle>(null)
  const decoderRef = useRef<TextDecoder>(makeDecoder())
  const isTerminal = kind === "terminal"

  // Logs view: true once the user scrolls up off the bottom, so we can offer a
  // "Jump to bottom" button. xterm keeps the viewport pinned on write unless
  // the user has scrolled away.
  const [scrolledUp, setScrolledUp] = useState(false)

  // Seed the logs view from the REST snapshot for instant first paint, before
  // the WS attaches. Disabled for the terminal.
  const { data: snapshot } = useQuery({
    queryKey: ["environment-logs-snapshot", environment.id, LOGS_TAIL],
    queryFn: () =>
      EnvironmentsService.getEnvironmentLogs({
        id: environment.id,
        lines: LOGS_TAIL,
      }),
    enabled: open && !isTerminal,
    staleTime: 0,
    gcTime: 0,
  })

  // Set once the first live WS frame arrives, so a late-resolving REST snapshot
  // doesn't paint stale tail lines *after* live output (out-of-order).
  const liveStartedRef = useRef(false)

  const handleMessage = useCallback((data: ArrayBuffer | string) => {
    const term = consoleRef.current
    if (!term) return
    liveStartedRef.current = true
    if (typeof data === "string") {
      term.write(data)
    } else {
      // Decode bytes (terminal screen output / any binary log frames) as UTF-8.
      term.write(decoderRef.current.decode(new Uint8Array(data), { stream: true }))
    }
  }, [])

  const handleOpen = useCallback(() => {
    // Fresh decoder per connection so a reconnect doesn't carry a partial rune.
    decoderRef.current = makeDecoder()
  }, [])

  const {
    status,
    closeReason,
    reconnectAttempt,
    sendBytes,
    sendControl,
    reconnect,
  } = useEnvConsoleSocket({
    environmentId: environment.id,
    kind,
    tail: isTerminal ? undefined : LOGS_TAIL,
    enabled: open,
    onMessage: handleMessage,
    onOpen: handleOpen,
  })

  // Paint the REST snapshot once it arrives (logs only). Written before/around
  // the live stream; duplicate tail lines are acceptable for an operator view.
  const paintedSnapshotRef = useRef(false)
  useEffect(() => {
    if (isTerminal || paintedSnapshotRef.current || liveStartedRef.current) return
    // `GET /environments/{id}/logs` returns `{ logs: Array<string> }` (one line
    // per element). The generated client types the body as an open record, so
    // we narrow at runtime and tolerate a string fallback just in case.
    const raw = (snapshot as { logs?: string[] | string } | undefined)?.logs
    const text = Array.isArray(raw) ? raw.join("\n") : raw
    if (typeof text === "string" && text.length > 0) {
      consoleRef.current?.write(text.endsWith("\n") ? text : `${text}\n`)
      paintedSnapshotRef.current = true
    }
  }, [snapshot, isTerminal])

  // Reset per-open transient state when the drawer closes.
  useEffect(() => {
    if (!open) {
      paintedSnapshotRef.current = false
      liveStartedRef.current = false
      setScrolledUp(false)
    }
  }, [open])

  // Terminal: forward keystrokes as binary frames.
  const handleData = useCallback(
    (data: string) => {
      sendBytes(encoder.encode(data))
    },
    [sendBytes],
  )

  // Terminal: send a resize control frame when the grid reflows.
  const handleResize = useCallback(
    (size: { cols: number; rows: number }) => {
      if (!isTerminal) return
      sendControl({ type: "resize", cols: size.cols, rows: size.rows })
    },
    [isTerminal, sendControl],
  )

  const jumpToBottom = useCallback(() => {
    consoleRef.current?.scrollToBottom()
    setScrolledUp(false)
  }, [])

  const handleScrollPinnedChange = useCallback((pinned: boolean) => {
    setScrolledUp(!pinned)
  }, [])

  const { dot, label } = statusMeta(status)
  const banner = status !== "open" ? closeReasonCopy(closeReason, kind) : null
  const showReconnect = status === "closed" || status === "error"

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-full p-0 sm:max-w-3xl"
        // The console captures focus; let users type without the radix focus trap fighting them.
        onOpenAutoFocus={(e) => e.preventDefault()}
      >
        <div className="flex h-full flex-col">
          <SheetHeader className="border-b">
            <div className="flex items-center justify-between gap-3 pr-8">
              <div className="min-w-0">
                <SheetTitle className="flex items-center gap-2 truncate">
                  {isTerminal ? "Terminal" : "Logs"}
                  <span className="text-muted-foreground truncate text-sm font-normal">
                    {environment.instance_name}
                  </span>
                </SheetTitle>
                <div className="mt-1 flex items-center gap-2">
                  <EnvironmentStatusBadge status={environment.status} />
                  <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <span className={cn("h-2 w-2 rounded-full", dot)} />
                    {label}
                    {reconnectAttempt > 0 && status === "connecting" && (
                      <span>· retry {reconnectAttempt}</span>
                    )}
                  </span>
                </div>
              </div>
            </div>
          </SheetHeader>

          {isTerminal && (
            <div className="flex items-start gap-2 border-b bg-amber-50 px-4 py-2 text-xs text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                Full shell access — commands run as the agent and can read its
                credentials. Activity is audited.
              </span>
            </div>
          )}

          {banner && (
            <div className="flex items-start gap-2 border-b bg-red-50 px-4 py-2 text-xs text-red-900 dark:bg-red-950/30 dark:text-red-200">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{banner}</span>
            </div>
          )}

          <div className="relative min-h-0 flex-1 bg-[#0b0f17] p-2">
            <XtermConsole
              ref={consoleRef}
              readOnly={!isTerminal}
              onData={isTerminal ? handleData : undefined}
              onResize={isTerminal ? handleResize : undefined}
              onScrollPinnedChange={isTerminal ? undefined : handleScrollPinnedChange}
              className="h-full w-full"
            />
            {!isTerminal && scrolledUp && (
              <Button
                size="sm"
                variant="secondary"
                onClick={jumpToBottom}
                className="absolute bottom-4 right-4 gap-1 shadow-md"
              >
                <ArrowDownToLine className="h-4 w-4" />
                Jump to bottom
              </Button>
            )}
          </div>

          <div className="flex items-center justify-between gap-2 border-t p-3">
            <div className="text-xs text-muted-foreground">
              {isTerminal
                ? "Resize the drawer to reflow the terminal."
                : "Following container output."}
            </div>
            <div className="flex items-center gap-2">
              {showReconnect && (
                <Button size="sm" variant="outline" onClick={reconnect} className="gap-1">
                  <RotateCw className="h-4 w-4" />
                  Reconnect
                </Button>
              )}
              <Button
                size="sm"
                variant="ghost"
                onClick={() => onOpenChange(false)}
                className="gap-1"
              >
                <X className="h-4 w-4" />
                Close
              </Button>
            </div>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  )
}
