/**
 * The host side of the dashboards bridge for `view: "html"` tiles.
 *
 * The page runs the kit's `protocols/dashboards-bridge.js` (injected server
 * side, byte for byte) in a frame sandboxed without `allow-same-origin`, so
 * its origin is `"null"`. Everything here follows that file's `host_contract`
 * (`dashboards.schema.json` › `x-web-view.bridge`) and plan §7 / §13:
 *
 * - A message is matched to its tile by `event.source === iframe.contentWindow`;
 *   the origin is never checked (it is always `"null"`).
 * - Every `ui/initialize` mints a new random nonce (returned in
 *   `_meta.cinna.nonce`), a new `page_doc`, drops readiness and the open-link
 *   latch, and `PUT`s page-state. Every other message must carry the current
 *   nonce; one that does not is dropped.
 * - Nothing is reset on the iframe's `load` event: a page that reloads itself
 *   says so with a new `ui/initialize`.
 * - Data goes out only after that document's `ui/notifications/initialized`,
 *   posted with targetOrigin `'*'` (the only target an opaque origin has).
 */

import type { DashboardTileDataPublic } from "@/client"
import { isRecord } from "./componentSpec"
import type { PageContext } from "./dashboardFocus"
import { getHighlight, subscribeHighlight } from "./highlightStore"

const PROTOCOL_VERSION = "2026-01-26"
const PARAMS_MERGE_MS = 250
const NOT_OPENED = "The link was not opened."
const DECLINED = "The owner declined a link from this page."
const NO_CHAT = "No dashboard chat here."
/** The backend counts a page-state write older than 15 min as not ready. */
const PAGE_STATE_RESEND_MS = 10 * 60_000

export interface HostContext {
  theme: "light" | "dark"
  styles: { variables: Record<string, string> }
}

export interface BridgeInit {
  component: string
  title: string
  params: Record<string, unknown>
  size: string | null
}

/** A page's answer to `cinna/collect-context` (the bridge builds it itself). */
export interface CollectedContext {
  source: "set_context" | "collected"
  summary: string
  selection: string[]
  visible: string[]
  microdata: Array<{
    id?: string
    type?: string
    properties: Record<string, string | null>
  }>
  selected_text: string
}

export interface BridgeCallbacks {
  init: () => BridgeInit
  hostContext: () => HostContext
  /** The tile's stored param set, the base a page's partial change merges over. */
  storedParams: () => Record<string, unknown>
  setParams: (params: Record<string, unknown>) => void
  callEndpoint: (
    name: string,
    args: Record<string, unknown>,
  ) => Promise<unknown>
  /** The §3 code (env trouble as `call_failed`) and sentence for a rejected `callEndpoint`. */
  callError: (error: unknown) => { code: string; message: string }
  setPageState: (
    state: {
      page_doc: string | null
      highlight_ready: boolean
      /** Apply only while the stored doc is this one (another tab may own it now). */
      if_page_doc?: string
    },
    options?: { keepalive?: boolean },
  ) => void
  onModelContext: (context: PageContext) => void
  /** `false` when there is no dashboard chat to fill (fullscreen). */
  onMessage: (text: string) => boolean
  /** Resolves `true` when the owner confirms; `null` when a confirm is already open. */
  confirmLink: (url: string) => Promise<boolean> | null
  onInitialized: () => void
}

type JsonRpcId = string | number

interface JsonRpcMessage {
  jsonrpc: "2.0"
  id?: JsonRpcId | null
  method?: string
  params?: unknown
  result?: unknown
  error?: unknown
}

function isJsonRpc(value: unknown): value is JsonRpcMessage {
  return isRecord(value) && value.jsonrpc === "2.0"
}

function nonceOf(container: unknown): string | null {
  const meta = isRecord(container) ? container._meta : undefined
  const cinna = isRecord(meta) ? meta.cinna : undefined
  const nonce = isRecord(cinna) ? cinna.nonce : undefined
  return typeof nonce === "string" ? nonce : null
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string" && v !== "")
    : []
}

function randomId(): string {
  return crypto.randomUUID()
}

/** Only http, https and mailto open (plan §13 §4). */
export function linkKind(url: string): "web" | "mail" | null {
  try {
    const parsed = new URL(url)
    if (parsed.protocol === "http:" || parsed.protocol === "https:")
      return "web"
    if (parsed.protocol === "mailto:") return "mail"
  } catch {
    return null
  }
  return null
}

export function openExternal(url: string): void {
  const anchor = document.createElement("a")
  anchor.href = url
  if (linkKind(url) === "web") {
    anchor.target = "_blank"
    anchor.rel = "noopener noreferrer"
  }
  anchor.click()
}

// ── One window listener for every tile frame ─────────────────────────────

const bySource = new Map<MessageEventSource, FrameBridge>()
const byBlock = new Map<string, FrameBridge>()
let listening = false

function onWindowMessage(event: MessageEvent) {
  if (!event.source) return
  const bridge = bySource.get(event.source)
  if (bridge) bridge.receive(event.data)
}

function ensureListener() {
  if (listening) return
  window.addEventListener("message", onWindowMessage)
  listening = true
}

/**
 * The html page of one tile, for P7's composer: its own context answer, or
 * `null` when there is no such frame, no answer within `timeoutMs`, or an error.
 */
export function collectTileContext(
  blockId: string,
  timeoutMs = 500,
): Promise<CollectedContext | null> {
  const bridge = byBlock.get(blockId)
  return bridge ? bridge.collectContext(timeoutMs) : Promise.resolve(null)
}

export class FrameBridge {
  private frameWindow: Window | null = null
  private nonce: string | null = null
  private pageDoc: string | null = null
  private initialized = false
  private highlightReady = false
  private linkDeclined = false
  private lastData: DashboardTileDataPublic | null = null
  private lastSentKey: string | null = null
  private pendingParams: Record<string, unknown> | null = null
  private paramsTimer: ReturnType<typeof setTimeout> | null = null
  private nextHostId = 1
  private hostRequests = new Map<
    string,
    { resolve: (value: unknown) => void; timer: ReturnType<typeof setTimeout> }
  >()
  private unsubscribeHighlight: (() => void) | null = null
  private resendTimer: ReturnType<typeof setInterval> | null = null
  private readonly onPageHide = () => this.withdrawPageState()
  private readonly onPageShow = (event: PageTransitionEvent) => {
    // Back from the bfcache: the same document is on screen again.
    if (event.persisted) this.sendPageState()
  }

  constructor(
    private readonly blockId: string,
    private readonly callbacks: BridgeCallbacks,
  ) {}

  attach(iframe: HTMLIFrameElement): void {
    const target = iframe.contentWindow
    if (!target) return
    ensureListener()
    this.frameWindow = target
    bySource.set(target, this)
    byBlock.set(this.blockId, this)
    this.unsubscribeHighlight = subscribeHighlight(this.blockId, () =>
      this.sendHighlight(),
    )
    window.addEventListener("pagehide", this.onPageHide)
    window.addEventListener("pageshow", this.onPageShow)
    this.resendTimer = setInterval(() => {
      if (this.pageDoc) this.sendPageState()
    }, PAGE_STATE_RESEND_MS)
  }

  detach(): void {
    window.removeEventListener("pagehide", this.onPageHide)
    window.removeEventListener("pageshow", this.onPageShow)
    if (this.resendTimer) clearInterval(this.resendTimer)
    this.resendTimer = null
    this.withdrawPageState()
    if (this.frameWindow) bySource.delete(this.frameWindow)
    if (byBlock.get(this.blockId) === this) byBlock.delete(this.blockId)
    this.unsubscribeHighlight?.()
    this.unsubscribeHighlight = null
    if (this.paramsTimer) clearTimeout(this.paramsTimer)
    for (const pending of this.hostRequests.values()) {
      clearTimeout(pending.timer)
      pending.resolve(null)
    }
    this.hostRequests.clear()
    this.frameWindow = null
  }

  /** The tile's current data; sent now if the document is initialized, else on `initialized`. */
  pushData(data: DashboardTileDataPublic): void {
    this.lastData = data
    // The 30 s re-read returns the same result as a new object: send a
    // result once, not once per read.
    if (this.initialized && dataKey(data) !== this.lastSentKey)
      this.sendData(data)
  }

  pushHostContext(context: HostContext): void {
    if (this.initialized) {
      this.notify("ui/notifications/host-context-changed", { ...context })
    }
  }

  collectContext(timeoutMs: number): Promise<CollectedContext | null> {
    if (!this.initialized) return Promise.resolve(null)
    return this.request("cinna/collect-context", {}, timeoutMs).then(
      (result) =>
        isRecord(result) ? (result as unknown as CollectedContext) : null,
    )
  }

  // ── Inbound ────────────────────────────────────────────────────────────

  receive(raw: unknown): void {
    if (!isJsonRpc(raw)) return
    if (raw.method === "ui/initialize" && raw.id != null) {
      this.handleInitialize(raw.id)
      return
    }
    if (this.nonce === null) return

    if (!raw.method) {
      this.handleResponse(raw)
      return
    }
    if (nonceOf(raw.params) !== this.nonce) return

    const params = isRecord(raw.params) ? raw.params : {}
    if (raw.id == null) {
      this.handleNotification(raw.method, params)
    } else {
      void this.handleRequest(raw.id, raw.method, params)
    }
  }

  private handleInitialize(id: JsonRpcId) {
    this.nonce = randomId()
    this.pageDoc = randomId()
    this.initialized = false
    this.highlightReady = false
    this.linkDeclined = false
    this.sendPageState()
    const init = this.callbacks.init()
    this.reply(id, {
      protocolVersion: PROTOCOL_VERSION,
      hostInfo: { name: "cinna-core", version: "1" },
      hostCapabilities: {},
      hostContext: this.callbacks.hostContext(),
      _meta: {
        cinna: {
          nonce: this.nonce,
          component: init.component,
          title: init.title,
          params: init.params,
          size: init.size,
        },
      },
    })
  }

  private handleResponse(message: JsonRpcMessage) {
    const key = String(message.id)
    const pending = this.hostRequests.get(key)
    if (!pending) return
    const carrier = message.error
      ? isRecord(message.error)
        ? message.error.data
        : undefined
      : message.result
    if (nonceOf(carrier) !== this.nonce) return
    this.hostRequests.delete(key)
    clearTimeout(pending.timer)
    pending.resolve(message.error ? null : message.result)
  }

  private handleNotification(method: string, params: Record<string, unknown>) {
    switch (method) {
      case "ui/notifications/initialized":
        this.initialized = true
        this.callbacks.onInitialized()
        if (this.lastData) this.sendData(this.lastData)
        return
      case "cinna/params-changed":
        this.mergeParams(isRecord(params.params) ? params.params : {})
        return
      case "cinna/highlight-ready":
        if (!this.pageDoc) return
        this.highlightReady = true
        this.sendPageState()
        this.sendHighlight()
        return
      default:
        return
    }
  }

  private async handleRequest(
    id: JsonRpcId,
    method: string,
    params: Record<string, unknown>,
  ) {
    switch (method) {
      case "ping":
      case "ui/resource-teardown":
        this.reply(id, {})
        return
      case "tools/call":
        await this.handleToolCall(id, params)
        return
      case "ui/update-model-context": {
        const structured = isRecord(params.structuredContent)
          ? params.structuredContent
          : {}
        this.callbacks.onModelContext({
          summary:
            typeof structured.summary === "string" ? structured.summary : "",
          selection: stringList(structured.selection),
          visible: stringList(structured.visible),
        })
        this.reply(id, {})
        return
      }
      case "ui/message": {
        const content = Array.isArray(params.content) ? params.content : []
        const first = content.find(
          (c): c is { type: "text"; text: string } =>
            isRecord(c) && c.type === "text" && typeof c.text === "string",
        )
        if (this.callbacks.onMessage(first?.text ?? "")) this.reply(id, {})
        else this.replyError(id, NO_CHAT)
        return
      }
      case "ui/open-link":
        await this.handleOpenLink(
          id,
          typeof params.url === "string" ? params.url : "",
        )
        return
      default:
        this.replyError(id, `Method not found: ${method}`, -32601)
    }
  }

  private async handleToolCall(id: JsonRpcId, params: Record<string, unknown>) {
    const name = typeof params.name === "string" ? params.name : ""
    const args = isRecord(params.arguments) ? params.arguments : {}
    try {
      const value = await this.callbacks.callEndpoint(name, args)
      this.reply(id, toolResult(value))
    } catch (error) {
      const { code, message } = this.callbacks.callError(error)
      this.reply(id, {
        isError: true,
        content: [{ type: "text", text: message }],
        structuredContent: { error: { code, message } },
      })
    }
  }

  private async handleOpenLink(id: JsonRpcId, url: string) {
    if (this.linkDeclined) {
      this.replyError(id, DECLINED)
      return
    }
    if (!linkKind(url)) {
      this.replyError(
        id,
        `${NOT_OPENED} Only http, https and mailto links open.`,
      )
      return
    }
    const doc = this.pageDoc
    const decision = this.callbacks.confirmLink(url)
    if (!decision) {
      this.replyError(id, NOT_OPENED)
      return
    }
    const confirmed = await decision
    // A reload while the owner was deciding: the answer is for a page that is gone.
    if (doc !== this.pageDoc) return
    if (confirmed) {
      openExternal(url)
      this.reply(id, {})
    } else {
      this.linkDeclined = true
      this.replyError(id, NOT_OPENED)
    }
  }

  private mergeParams(partial: Record<string, unknown>) {
    this.pendingParams = { ...(this.pendingParams ?? {}), ...partial }
    if (this.paramsTimer) clearTimeout(this.paramsTimer)
    this.paramsTimer = setTimeout(() => {
      this.paramsTimer = null
      const merged = { ...this.callbacks.storedParams(), ...this.pendingParams }
      this.pendingParams = null
      // A cleared key goes back to its default: it is left out of the set.
      const full: Record<string, unknown> = {}
      for (const [key, value] of Object.entries(merged)) {
        if (value !== null && value !== undefined && value !== "")
          full[key] = value
      }
      this.callbacks.setParams(full)
    }, PARAMS_MERGE_MS)
  }

  // ── Outbound ───────────────────────────────────────────────────────────

  private sendPageState() {
    if (!this.pageDoc) return
    this.callbacks.setPageState({
      page_doc: this.pageDoc,
      highlight_ready: this.highlightReady,
    })
  }

  /** The document is gone (tile removed, tab closed or reloaded): not ready. */
  private withdrawPageState() {
    if (!this.pageDoc) return
    this.callbacks.setPageState(
      { page_doc: null, highlight_ready: false, if_page_doc: this.pageDoc },
      { keepalive: true },
    )
  }

  private sendData(data: DashboardTileDataPublic) {
    this.lastSentKey = dataKey(data)
    this.notify("ui/notifications/tool-result", {
      ...toolResult(data.data ?? null),
      _meta: {
        cinna: {
          stale: data.stale === true,
          fetched_at: data.fetched_at || null,
          nonce: this.nonce,
        },
      },
    })
  }

  private sendHighlight() {
    if (!this.initialized || !this.highlightReady) return
    this.notify("cinna/highlight", { ids: getHighlight(this.blockId).ids })
  }

  private post(message: JsonRpcMessage) {
    this.frameWindow?.postMessage(message, "*")
  }

  private withNonce(params: Record<string, unknown>) {
    const meta = isRecord(params._meta) ? params._meta : {}
    const cinna = isRecord(meta.cinna) ? meta.cinna : {}
    return {
      ...params,
      _meta: { ...meta, cinna: { ...cinna, nonce: this.nonce } },
    }
  }

  private notify(method: string, params: Record<string, unknown>) {
    this.post({ jsonrpc: "2.0", method, params: this.withNonce(params) })
  }

  private reply(id: JsonRpcId, result: Record<string, unknown>) {
    this.post({ jsonrpc: "2.0", id, result: this.withNonce(result) })
  }

  private replyError(id: JsonRpcId, message: string, code = -32000) {
    this.post({
      jsonrpc: "2.0",
      id,
      error: { code, message, data: this.withNonce({}) },
    })
  }

  private request(
    method: string,
    params: Record<string, unknown>,
    timeoutMs: number,
  ): Promise<unknown> {
    const id = `host-${this.nextHostId++}`
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.hostRequests.delete(id)
        resolve(null)
      }, timeoutMs)
      this.hostRequests.set(id, { resolve, timer })
      this.post({ jsonrpc: "2.0", id, method, params: this.withNonce(params) })
    })
  }
}

function dataKey(data: DashboardTileDataPublic): string {
  return `${data.fetched_at || ""}|${data.stale === true}`
}

/** An MCP CallToolResult for a JSON value (structuredContent must be an object). */
function toolResult(value: unknown): Record<string, unknown> {
  if (isRecord(value)) return { structuredContent: value, content: [] }
  return { content: [{ type: "text", text: JSON.stringify(value ?? null) }] }
}
