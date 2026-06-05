/**
 * XtermConsole — thin React wrapper around an xterm.js terminal.
 *
 * Mounts an {@link Terminal} instance into a ref'd <div>, wires the
 * {@link FitAddon} + a {@link ResizeObserver} so the grid reflows to the
 * container, and exposes an imperative handle ({@link XtermConsoleHandle})
 * with `write` / `clear` / `focus` plus the current `cols`/`rows`.
 *
 * The same component backs both consoles:
 *   * Terminal  — interactive; `onData` keystrokes flow to the caller, and
 *     `onResize` reports new cols/rows so the caller can send a resize control
 *     frame to the backend PTY.
 *   * Logs      — `readOnly` (xterm `disableStdin`); the caller only `write()`s
 *     incoming log lines; keystrokes are ignored.
 *
 * The component owns no socket logic — it is purely the renderer. The
 * {@link useEnvConsoleSocket} hook owns the WebSocket; the
 * {@link EnvironmentConsoleDrawer} stitches the two together.
 */
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
} from "react"
import { Terminal } from "@xterm/xterm"
import { FitAddon } from "@xterm/addon-fit"
import { WebLinksAddon } from "@xterm/addon-web-links"
import "@xterm/xterm/css/xterm.css"

export interface XtermConsoleHandle {
  /** Write a chunk (string or bytes) to the terminal screen. */
  write: (data: string | Uint8Array) => void
  /** Clear the terminal scrollback + viewport. */
  clear: () => void
  /** Focus the terminal so keystrokes are captured. */
  focus: () => void
  /** Scroll the viewport to the bottom (logs "Jump to bottom"). */
  scrollToBottom: () => void
  /** Current grid size (after the last fit). */
  getSize: () => { cols: number; rows: number }
}

interface XtermConsoleProps {
  /**
   * When true the terminal is write-only (logs view): stdin is disabled and
   * the cursor is hidden. Defaults to false (interactive terminal).
   */
  readOnly?: boolean
  /** Called with raw keystroke data; only fires when not `readOnly`. */
  onData?: (data: string) => void
  /**
   * Called (debounced) whenever the fit addon recomputes the grid size, e.g.
   * after a container resize. The interactive terminal uses this to send a
   * resize control frame to the backend PTY.
   */
  onResize?: (size: { cols: number; rows: number }) => void
  /**
   * Reports whether the viewport is pinned to the bottom (true) or the user
   * has scrolled up (false). The logs view uses this to show "Jump to bottom".
   */
  onScrollPinnedChange?: (pinned: boolean) => void
  /** Enable xterm's screen-reader accessibility mode. */
  screenReaderMode?: boolean
  className?: string
}

export const XtermConsole = forwardRef<XtermConsoleHandle, XtermConsoleProps>(
  function XtermConsole(
    {
      readOnly = false,
      onData,
      onResize,
      onScrollPinnedChange,
      screenReaderMode = false,
      className,
    },
    ref,
  ) {
    const containerRef = useRef<HTMLDivElement | null>(null)
    const termRef = useRef<Terminal | null>(null)
    const fitRef = useRef<FitAddon | null>(null)
    // Latest callbacks held in refs so the mount effect can stay
    // dependency-free (we never want to tear down / re-create the terminal
    // just because a parent re-rendered with a new closure).
    const onDataRef = useRef(onData)
    const onResizeRef = useRef(onResize)
    const onScrollPinnedChangeRef = useRef(onScrollPinnedChange)
    const resizeRafRef = useRef<number | null>(null)

    useEffect(() => {
      onDataRef.current = onData
      onResizeRef.current = onResize
      onScrollPinnedChangeRef.current = onScrollPinnedChange
    }, [onData, onResize, onScrollPinnedChange])

    useImperativeHandle(
      ref,
      () => ({
        write: (data) => termRef.current?.write(data),
        clear: () => termRef.current?.clear(),
        focus: () => termRef.current?.focus(),
        scrollToBottom: () => termRef.current?.scrollToBottom(),
        getSize: () => ({
          cols: termRef.current?.cols ?? 80,
          rows: termRef.current?.rows ?? 24,
        }),
      }),
      [],
    )

    useEffect(() => {
      const container = containerRef.current
      if (!container) return

      const term = new Terminal({
        convertEol: true,
        cursorBlink: !readOnly,
        disableStdin: readOnly,
        fontFamily:
          'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace',
        fontSize: 13,
        scrollback: 5000,
        screenReaderMode,
        theme: {
          background: "#0b0f17",
          foreground: "#d6deeb",
          cursor: readOnly ? "#0b0f17" : "#d6deeb",
        },
      })
      const fit = new FitAddon()
      term.loadAddon(fit)
      term.loadAddon(new WebLinksAddon())
      term.open(container)

      // Initial fit (deferred a frame so the container has laid out).
      const initialFit = requestAnimationFrame(() => {
        try {
          fit.fit()
          onResizeRef.current?.({ cols: term.cols, rows: term.rows })
        } catch {
          /* container not measurable yet — ResizeObserver will retry */
        }
      })

      const dataDisposable = term.onData((data) => {
        if (!readOnly) onDataRef.current?.(data)
      })

      // Report whether the viewport is pinned to the bottom so the logs view
      // can offer a "Jump to bottom" affordance when the user scrolls up.
      const scrollDisposable = term.onScroll((top) => {
        const buffer = term.buffer.active
        const maxTop = buffer.length - term.rows
        onScrollPinnedChangeRef.current?.(top >= maxTop)
      })

      // Reflow on container resize, coalesced to one fit per animation frame.
      const observer = new ResizeObserver(() => {
        if (resizeRafRef.current !== null) return
        resizeRafRef.current = requestAnimationFrame(() => {
          resizeRafRef.current = null
          try {
            fit.fit()
            onResizeRef.current?.({ cols: term.cols, rows: term.rows })
          } catch {
            /* ignore transient measurement errors */
          }
        })
      })
      observer.observe(container)

      termRef.current = term
      fitRef.current = fit

      return () => {
        cancelAnimationFrame(initialFit)
        if (resizeRafRef.current !== null) {
          cancelAnimationFrame(resizeRafRef.current)
          resizeRafRef.current = null
        }
        observer.disconnect()
        dataDisposable.dispose()
        scrollDisposable.dispose()
        term.dispose()
        termRef.current = null
        fitRef.current = null
      }
      // Intentionally mount-once: readOnly/screenReaderMode are fixed per
      // drawer instance (Terminal vs Logs), so re-creating on change is fine
      // but never happens in practice.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [readOnly, screenReaderMode])

    return (
      <div
        ref={containerRef}
        className={className}
        aria-label={readOnly ? "Container logs" : "Interactive terminal"}
        role="group"
      />
    )
  },
)
