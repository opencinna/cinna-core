/**
 * The browser's user session: an in-memory access token backed by an
 * HttpOnly refresh cookie (`cinna_refresh`) that only the backend can read.
 *
 * Invariant: **no user JWT is ever written to Web Storage.** The only thing
 * persisted is the session's CSRF token (`localStorage["cinna_csrf"]`), which
 * is useless without the cookie and is what tells `bootstrapSession` that a
 * cookie session may exist. A reload restores the access token by calling
 * `POST /login/session/refresh` with that CSRF token.
 *
 * Refreshes are single-flight within the tab (one shared promise) and across
 * tabs (`navigator.locks`, plus `BroadcastChannel("cinna-auth")` so a tab
 * that waited for the lock reuses the token another tab just obtained).
 * Logout is broadcast on the same channel.
 *
 * This module has no dependency on the generated client so it runs under
 * `node --test`; `authSessionClient.ts` supplies the HTTP transport.
 */

export interface AuthSessionTransport {
  /** Resolves the new access token; rejects with `{ status }` on HTTP errors. */
  refresh(csrfToken: string): Promise<string>
  logout(csrfToken: string): Promise<void>
}

export type RefreshOutcome =
  | { kind: "ok"; token: string }
  /** The cookie session is gone (401 `session_expired`); local state is cleared. */
  | { kind: "expired" }
  /** Network error, 5xx, `csrf_failed`: inconclusive, nothing is cleared. */
  | { kind: "failed" }

type ChannelMessage = { type: "token"; token: string } | { type: "logout" }

export const CSRF_STORAGE_KEY = "cinna_csrf"
export const GUEST_TOKEN_STORAGE_KEY = "guest_access_token"
const LEGACY_TOKEN_STORAGE_KEY = "access_token"
const CHANNEL_NAME = "cinna-auth"
const LOCK_NAME = "cinna-refresh"

/** A token closer than this to its `exp` is refreshed before it is sent. */
export const FRESHNESS_MARGIN_MS = 30_000
/** S0: a backend that is down must never leave a blank page. */
export const BOOTSTRAP_TIMEOUT_MS = 5_000
/** Logout waits this long for the server before the caller navigates away. */
export const LOGOUT_WAIT_MS = 2_000

let accessToken: string | null = null
let transport: AuthSessionTransport | null = null
let channel: BroadcastChannel | null = null
let inflight: Promise<RefreshOutcome> | null = null
let remoteLogoutHandler: (() => void) | null = null
/** The last refresh ended `failed` (5xx, network, `csrf_failed`). */
let refreshInconclusive = false

// ── Storage (guarded: absent in node tests and some sandboxed iframes) ──────

function local(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage
  } catch {
    return null
  }
}

function session(): Storage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage
  } catch {
    return null
  }
}

export function getCsrfToken(): string | null {
  return local()?.getItem(CSRF_STORAGE_KEY) || null
}

// ── Cross-tab channel ───────────────────────────────────────────────────────

function post(message: ChannelMessage): void {
  try {
    channel?.postMessage(message)
  } catch {
    // A closed channel only loses the cross-tab hint; each tab still recovers
    // on its own through its next refresh.
  }
}

function onChannelMessage(event: MessageEvent<ChannelMessage>): void {
  const message = event.data
  if (message?.type === "token" && typeof message.token === "string") {
    // Another tab rotated the shared cookie. Adopt its token only while this
    // tab is part of the same session; a signed-out tab stays signed out.
    if (accessToken !== null || getCsrfToken() !== null) {
      accessToken = message.token
    }
  } else if (message?.type === "logout") {
    accessToken = null
    remoteLogoutHandler?.()
  }
}

/**
 * Wire the HTTP transport and the handler run when another tab logs out.
 * Safe to call again (tests): the previous channel is closed.
 */
export function configureAuthSession(options: {
  transport: AuthSessionTransport
  onRemoteLogout?: () => void
}): void {
  transport = options.transport
  remoteLogoutHandler = options.onRemoteLogout ?? null
  channel?.close()
  channel = null
  if (typeof BroadcastChannel !== "undefined") {
    channel = new BroadcastChannel(CHANNEL_NAME)
    channel.onmessage = onChannelMessage
    // Node keeps the process alive for an open channel; browsers lack unref.
    if ("unref" in channel && typeof channel.unref === "function") {
      channel.unref()
    }
  }
}

// ── Token access ────────────────────────────────────────────────────────────

/** `exp` in seconds, decoded but not verified. Null when unreadable. */
export function decodeTokenExpiry(token: string): number | null {
  try {
    const payload = token.split(".")[1]
    if (!payload) return null
    const base64 = payload.replace(/-/g, "+").replace(/_/g, "/")
    const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4)
    const claims: unknown = JSON.parse(atob(padded))
    if (claims && typeof claims === "object" && "exp" in claims) {
      return typeof claims.exp === "number" ? claims.exp : null
    }
    return null
  } catch {
    return null
  }
}

function isFresh(token: string): boolean {
  const exp = decodeTokenExpiry(token)
  // An unreadable token is sent as is; the server is the judge.
  return exp === null || exp * 1000 - Date.now() > FRESHNESS_MARGIN_MS
}

/** The in-memory user access token, or null when signed out. Synchronous. */
export function getAccessToken(): string | null {
  return accessToken
}

/**
 * The user access token, refreshed first when it expires within 30 s.
 * Null when signed out (or when the refresh found the session expired).
 */
export async function getFreshAccessToken(): Promise<string | null> {
  const token = accessToken
  if (token === null || isFresh(token)) return token
  const outcome = await refreshSession()
  // "failed" is inconclusive: send what we have and let the server decide.
  return outcome.kind === "ok" ? outcome.token : accessToken
}

/** Start a session after a sign-in response. Persists only the CSRF token. */
export function setSession(token: string, csrfToken?: string | null): void {
  accessToken = token
  refreshInconclusive = false
  const storage = local()
  if (csrfToken) storage?.setItem(CSRF_STORAGE_KEY, csrfToken)
  storage?.removeItem(LEGACY_TOKEN_STORAGE_KEY)
}

/** Forget the session in this tab (memory and `cinna_csrf`). No broadcast. */
export function clearSession(): void {
  accessToken = null
  refreshInconclusive = false
  local()?.removeItem(CSRF_STORAGE_KEY)
}

/** Tell other tabs this browser signed out. */
export function broadcastLogout(): void {
  post({ type: "logout" })
}

// ── Refresh ─────────────────────────────────────────────────────────────────

function statusOf(error: unknown): number | undefined {
  if (error && typeof error === "object" && "status" in error) {
    return typeof error.status === "number" ? error.status : undefined
  }
  return undefined
}

async function withRefreshLock<T>(run: () => Promise<T>): Promise<T> {
  const locks = typeof navigator === "undefined" ? undefined : navigator.locks
  if (locks && typeof locks.request === "function") {
    return locks.request(LOCK_NAME, run)
  }
  return run()
}

/**
 * True when the last refresh ended `failed`: the cookie session may still be
 * alive, so a 401 that reached a page handler after it is inconclusive and
 * must not clear the session (that would orphan the cookie). Only an
 * `expired` refresh ends the session, and the store has already cleared it.
 */
export function isRefreshInconclusive(): boolean {
  return refreshInconclusive
}

async function performRefresh(): Promise<RefreshOutcome> {
  const csrfToken = getCsrfToken()
  if (!csrfToken || !transport) {
    // No CSRF token means no cookie session this tab could refresh.
    accessToken = null
    refreshInconclusive = false
    return { kind: "expired" }
  }
  try {
    const token = await transport.refresh(csrfToken)
    accessToken = token
    refreshInconclusive = false
    post({ type: "token", token })
    return { kind: "ok", token }
  } catch (error) {
    if (statusOf(error) === 401) {
      clearSession()
      return { kind: "expired" }
    }
    refreshInconclusive = true
    return { kind: "failed" }
  }
}

/**
 * Exchange the refresh cookie for a new access token. Concurrent callers in
 * this tab share one request; across tabs the lock serialises them, and a
 * tab that waited reuses the token the lock holder broadcast.
 */
export function refreshSession(): Promise<RefreshOutcome> {
  if (inflight) return inflight
  const tokenBefore = accessToken
  inflight = withRefreshLock(async (): Promise<RefreshOutcome> => {
    const current = accessToken
    if (current !== null && current !== tokenBefore && isFresh(current)) {
      return { kind: "ok", token: current }
    }
    return performRefresh()
  }).finally(() => {
    inflight = null
  })
  return inflight
}

/**
 * Recover after the server rejected `rejectedToken` with 401: reuse a token
 * that already replaced it (another request or tab refreshed meanwhile),
 * otherwise refresh.
 */
export function refreshAfterRejection(
  rejectedToken: string,
): Promise<RefreshOutcome> {
  const current = accessToken
  if (current !== null && current !== rejectedToken) {
    return Promise.resolve({ kind: "ok", token: current })
  }
  return refreshSession()
}

// ── Bootstrap and logout ────────────────────────────────────────────────────

/**
 * S0: run once before the first render. Removes any legacy
 * `localStorage["access_token"]` and, when a CSRF token says a cookie
 * session may exist, restores the access token from it. Settles within
 * {@link BOOTSTRAP_TIMEOUT_MS}; on timeout or a network error the app renders
 * signed out and `cinna_csrf` is kept so the next reload can retry.
 */
export async function bootstrapSession(
  timeoutMs: number = BOOTSTRAP_TIMEOUT_MS,
): Promise<void> {
  local()?.removeItem(LEGACY_TOKEN_STORAGE_KEY)
  if (accessToken !== null || getCsrfToken() === null) return
  let timer: ReturnType<typeof setTimeout> | undefined
  const timeout = new Promise<void>((resolve) => {
    timer = setTimeout(resolve, timeoutMs)
  })
  try {
    await Promise.race([refreshSession(), timeout])
  } finally {
    clearTimeout(timer)
  }
}

/**
 * End this browser's session: forget it locally (even if the server call
 * fails, so an orphaned cookie can never sign the person back in), tell other
 * tabs, and revoke the cookie session server-side. Waits at most
 * {@link LOGOUT_WAIT_MS} for the server so the caller's navigation is never
 * held up, but long enough that the request isn't cancelled by it.
 */
export async function logoutSession(
  waitMs: number = LOGOUT_WAIT_MS,
): Promise<void> {
  const csrfToken = getCsrfToken()
  clearSession()
  broadcastLogout()
  if (!csrfToken || !transport) return
  let timer: ReturnType<typeof setTimeout> | undefined
  const timeout = new Promise<void>((resolve) => {
    timer = setTimeout(resolve, waitMs)
  })
  try {
    await Promise.race([
      transport.logout(csrfToken).catch(() => undefined),
      timeout,
    ])
  } finally {
    clearTimeout(timer)
  }
}

// ── Guest share token (scoped, ≤ 24 h, per browser tab) ─────────────────────

/**
 * The guest-share JWT lives apart from the user session, in sessionStorage,
 * and never replaces a signed-in user's token: the bearer prefers the user.
 */
export function getGuestToken(): string | null {
  return session()?.getItem(GUEST_TOKEN_STORAGE_KEY) || null
}

export function setGuestToken(token: string): void {
  session()?.setItem(GUEST_TOKEN_STORAGE_KEY, token)
}

export function clearGuestToken(): void {
  session()?.removeItem(GUEST_TOKEN_STORAGE_KEY)
}

/** The bearer for API calls: the (fresh) user token, else a guest token. */
export async function resolveBearer(): Promise<string> {
  return (await getFreshAccessToken()) ?? getGuestToken() ?? ""
}
