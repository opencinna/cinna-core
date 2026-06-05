/**
 * useEnvConsoleSocket — raw WebSocket client for the environment consoles.
 *
 * The OpenAPI client (`@/client`) does not cover WebSockets, so this hook
 * manages a plain {@link WebSocket} for the two backend console endpoints:
 *
 *   * `terminal` → `WS /api/v1/env-console/{id}/terminal`
 *   * `logs`     → `WS /api/v1/env-console/{id}/logs/stream?tail=N`
 *
 * Browsers cannot set `Authorization` on a WS handshake, so the platform
 * access token is passed as a `?token=` query param (read from
 * `localStorage["access_token"]`), matching the backend
 * `get_env_console_context_ws` dependency.
 *
 * Wire contract (mirrors `EnvironmentConsoleService`):
 *   * Terminal — keystrokes are sent as **binary** frames; resize is a **text**
 *     JSON frame `{"type":"resize","cols","rows"}`. Screen output arrives as
 *     **binary**; an idle close arrives as text `{"type":"closed","reason":...}`.
 *   * Logs     — server→client **text** lines; the final text `{"type":"closed"}`
 *     signals the container stopped. Optional client→server text control frames.
 *
 * Reconnect uses a capped exponential backoff (like `eventService.ts`, but a
 * plain WebSocket — no Socket.IO). Application close codes (4404/4429/1008) are
 * surfaced as terminal `closeReason`s and suppress auto-reconnect, since they
 * are non-transient (env stopped, cap exceeded, unauthorized).
 */
import { useCallback, useEffect, useRef, useState } from "react"

import { OpenAPI } from "@/client"

export type EnvConsoleKind = "terminal" | "logs"

export type EnvConsoleStatus = "connecting" | "open" | "closed" | "error"

/** Why a socket closed, when we can attribute it to a known cause. */
export type EnvConsoleCloseReason =
  | "env_not_running" // 4404 — environment is not running
  | "concurrency_exceeded" // 4429 — too many open consoles
  | "unauthorized" // 1008 — token invalid / not permitted
  | "idle_timeout" // server idle close (terminal)
  | "container_stopped" // logs {"type":"closed"} — container exited
  | "normal" // clean close (drawer closed)
  | "error" // transport error / unexpected drop
  | null

// Application close codes defined by the backend console service.
const WS_CLOSE_ENV_NOT_RUNNING = 4404
const WS_CLOSE_CONCURRENCY_EXCEEDED = 4429
const WS_CLOSE_POLICY_VIOLATION = 1008
// 1011 = internal error: env-core "no shell available" / backend "cannot reach
// environment shell". Non-transient for a pre-rebuild env, so don't auto-retry.
const WS_CLOSE_INTERNAL_ERROR = 1011
const WS_CLOSE_NORMAL = 1000
const WS_CLOSE_GOING_AWAY = 1001

const MAX_RECONNECT_ATTEMPTS = 5
const BASE_RECONNECT_DELAY_MS = 1000
const MAX_RECONNECT_DELAY_MS = 8000

export interface UseEnvConsoleSocketOptions {
  environmentId: string
  kind: EnvConsoleKind
  /** Initial tail size for the logs stream (ignored for terminal). */
  tail?: number
  /** Whether the hook should hold a connection open. */
  enabled?: boolean
  /** Binary screen/log payload (decoded text for logs, raw bytes for terminal). */
  onMessage?: (data: ArrayBuffer | string) => void
  /** Fires once when the socket transitions to `open`. */
  onOpen?: () => void
}

export interface UseEnvConsoleSocketResult {
  status: EnvConsoleStatus
  closeReason: EnvConsoleCloseReason
  /** Number of reconnect attempts already spent (resets on a clean open). */
  reconnectAttempt: number
  /** Send raw bytes (terminal keystrokes). No-op unless the socket is open. */
  sendBytes: (data: Uint8Array) => void
  /** Send a JSON text control frame (e.g. resize / set_tail). */
  sendControl: (payload: Record<string, unknown>) => void
  /** Manually (re)connect — used by the drawer's "Reconnect" button. */
  reconnect: () => void
  /** Close the socket and stop reconnecting. */
  disconnect: () => void
}

/** Build the `ws(s)://…` console URL from the configured API base. */
function buildConsoleUrl(kind: EnvConsoleKind, environmentId: string, tail?: number): string {
  // OpenAPI.BASE is the http(s) API origin (VITE_API_URL); may be relative/empty.
  const base = (OpenAPI.BASE || window.location.origin).replace(/\/$/, "")
  const httpUrl = base.startsWith("http") ? base : `${window.location.origin}${base}`
  const wsBase = httpUrl.replace(/^http/, "ws")

  const path =
    kind === "terminal"
      ? `/api/v1/env-console/${environmentId}/terminal`
      : `/api/v1/env-console/${environmentId}/logs/stream`

  const params = new URLSearchParams()
  const token = localStorage.getItem("access_token")
  if (token) params.set("token", token)
  if (kind === "logs" && tail != null) params.set("tail", String(tail))

  const query = params.toString()
  return `${wsBase}${path}${query ? `?${query}` : ""}`
}

/** Map a close code to a non-transient reason (suppresses auto-reconnect). */
function reasonForCloseCode(code: number): EnvConsoleCloseReason {
  switch (code) {
    case WS_CLOSE_ENV_NOT_RUNNING:
      return "env_not_running"
    case WS_CLOSE_CONCURRENCY_EXCEEDED:
      return "concurrency_exceeded"
    case WS_CLOSE_POLICY_VIOLATION:
      return "unauthorized"
    case WS_CLOSE_NORMAL:
    case WS_CLOSE_GOING_AWAY:
      return "normal"
    default:
      return "error"
  }
}

/** Close codes that should NOT trigger an automatic reconnect. */
const NON_RETRYABLE_CODES = new Set<number>([
  WS_CLOSE_ENV_NOT_RUNNING,
  WS_CLOSE_CONCURRENCY_EXCEEDED,
  WS_CLOSE_POLICY_VIOLATION,
  WS_CLOSE_INTERNAL_ERROR,
  WS_CLOSE_NORMAL,
  WS_CLOSE_GOING_AWAY,
])

export function useEnvConsoleSocket(
  options: UseEnvConsoleSocketOptions,
): UseEnvConsoleSocketResult {
  const { environmentId, kind, tail, enabled = true, onMessage, onOpen } = options

  const [status, setStatus] = useState<EnvConsoleStatus>("connecting")
  const [closeReason, setCloseReason] = useState<EnvConsoleCloseReason>(null)
  const [reconnectAttempt, setReconnectAttempt] = useState(0)

  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const attemptRef = useRef(0)
  const manualCloseRef = useRef(false)
  // Keep callbacks in refs so connect() stays stable and doesn't churn the socket.
  const onMessageRef = useRef(onMessage)
  const onOpenRef = useRef(onOpen)

  useEffect(() => {
    onMessageRef.current = onMessage
    onOpenRef.current = onOpen
  }, [onMessage, onOpen])

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current)
      reconnectTimerRef.current = null
    }
  }, [])

  const connect = useCallback(() => {
    // Drop any prior socket before opening a new one.
    if (wsRef.current) {
      try {
        wsRef.current.close()
      } catch {
        /* already closed */
      }
      wsRef.current = null
    }
    clearReconnectTimer()

    setStatus("connecting")
    setCloseReason(null)

    let ws: WebSocket
    try {
      ws = new WebSocket(buildConsoleUrl(kind, environmentId, tail))
    } catch {
      setStatus("error")
      setCloseReason("error")
      return
    }
    ws.binaryType = "arraybuffer"
    wsRef.current = ws

    ws.onopen = () => {
      attemptRef.current = 0
      setReconnectAttempt(0)
      setStatus("open")
      setCloseReason(null)
      onOpenRef.current?.()
    }

    ws.onmessage = (event) => {
      // Logs arrive as text; terminal screen bytes as ArrayBuffer. Either way
      // we inspect text frames for a `{"type":"closed"}` control sentinel.
      if (typeof event.data === "string") {
        const sentinel = parseClosedSentinel(event.data)
        if (sentinel) {
          // Server announced an in-band close (idle timeout / container stop).
          setCloseReason(sentinel)
          // Let the subsequent onclose drive status; just remember the reason.
          return
        }
      }
      onMessageRef.current?.(event.data)
    }

    ws.onerror = () => {
      // onerror is always followed by onclose; record an error hint.
      setStatus("error")
    }

    ws.onclose = (event) => {
      wsRef.current = null
      const codeReason = reasonForCloseCode(event.code)

      setCloseReason((prev) => {
        // Preserve an in-band sentinel reason (idle/container) over the code.
        if (prev === "idle_timeout" || prev === "container_stopped") return prev
        return codeReason
      })
      setStatus("closed")

      if (manualCloseRef.current) {
        manualCloseRef.current = false
        return
      }

      // Non-retryable closes (env down, cap, unauthorized, clean) stop here.
      if (NON_RETRYABLE_CODES.has(event.code)) return

      // Transient drop → capped exponential backoff reconnect.
      if (attemptRef.current >= MAX_RECONNECT_ATTEMPTS) {
        setCloseReason("error")
        return
      }
      const delay = Math.min(
        BASE_RECONNECT_DELAY_MS * 2 ** attemptRef.current,
        MAX_RECONNECT_DELAY_MS,
      )
      attemptRef.current += 1
      setReconnectAttempt(attemptRef.current)
      setStatus("connecting")
      reconnectTimerRef.current = setTimeout(connect, delay)
    }
  }, [clearReconnectTimer, environmentId, kind, tail])

  const reconnect = useCallback(() => {
    attemptRef.current = 0
    setReconnectAttempt(0)
    manualCloseRef.current = false
    connect()
  }, [connect])

  const disconnect = useCallback(() => {
    manualCloseRef.current = true
    clearReconnectTimer()
    if (wsRef.current) {
      try {
        wsRef.current.close(WS_CLOSE_NORMAL, "client closed")
      } catch {
        /* already closed */
      }
      wsRef.current = null
    }
  }, [clearReconnectTimer])

  const sendBytes = useCallback((data: Uint8Array) => {
    const ws = wsRef.current
    if (ws?.readyState === WebSocket.OPEN) ws.send(data)
  }, [])

  const sendControl = useCallback((payload: Record<string, unknown>) => {
    const ws = wsRef.current
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload))
  }, [])

  // Open/teardown lifecycle. Reconnects when the target (id/kind) changes or
  // the hook is toggled via `enabled`.
  useEffect(() => {
    if (!enabled) {
      disconnect()
      return
    }
    manualCloseRef.current = false
    attemptRef.current = 0
    connect()
    return () => {
      manualCloseRef.current = true
      clearReconnectTimer()
      if (wsRef.current) {
        try {
          wsRef.current.close(WS_CLOSE_NORMAL, "unmount")
        } catch {
          /* already closed */
        }
        wsRef.current = null
      }
    }
  }, [enabled, connect, disconnect, clearReconnectTimer])

  return {
    status,
    closeReason,
    reconnectAttempt,
    sendBytes,
    sendControl,
    reconnect,
    disconnect,
  }
}

/**
 * Inspect a text frame for the server's in-band close sentinel and translate
 * it to a {@link EnvConsoleCloseReason}. Returns null for ordinary text lines.
 */
function parseClosedSentinel(text: string): EnvConsoleCloseReason {
  // Fast bail-out: only JSON objects mentioning "closed" can be sentinels.
  if (!text.includes('"closed"')) return null
  try {
    const parsed = JSON.parse(text)
    if (parsed?.type !== "closed") return null
    if (parsed.reason === "idle_timeout") return "idle_timeout"
    return "container_stopped"
  } catch {
    return null
  }
}
