import { AxiosError } from "axios"
import type { ApiError } from "./client"
import { OpenAPI, UsersService } from "./client"

export const APP_NAME = import.meta.env.VITE_APP_NAME || "Cinna"

/**
 * Fire-and-forget: persist the browser-detected timezone/language/locale to the
 * current user, filling only fields that are still NULL server-side. Called
 * right after a token is stored on browser login (password + Google OAuth).
 *
 * The server enforces the NULL-only guarantee, so it is safe to send detected
 * values on every login — an explicit choice the user made in Settings is never
 * overwritten. Best-effort: all errors are swallowed and it never blocks
 * navigation. Detection is guarded so unsupported environments simply send less.
 */
export const persistDetectedLocaleDefaults = (): void => {
  try {
    const timezone =
      typeof Intl !== "undefined" && typeof Intl.DateTimeFormat === "function"
        ? Intl.DateTimeFormat().resolvedOptions().timeZone
        : undefined
    const fullLocale =
      typeof navigator !== "undefined" ? navigator.language : undefined
    const language = fullLocale ? fullLocale.split("-")[0] : undefined

    const requestBody: {
      timezone?: string | null
      language?: string | null
      locale?: string | null
    } = {}
    if (timezone) requestBody.timezone = timezone
    if (language) requestBody.language = language
    if (fullLocale) requestBody.locale = fullLocale

    if (Object.keys(requestBody).length === 0) return

    void UsersService.updateUserLocaleDefaults({ requestBody }).catch(() => {
      // Best-effort: detection/persistence failures must not affect login.
    })
  } catch {
    // Detection itself failed (very old browser) — ignore.
  }
}

function extractErrorMessage(err: ApiError): string {
  if (err instanceof AxiosError) {
    return err.message
  }

  const errDetail = (err.body as any)?.detail
  if (Array.isArray(errDetail) && errDetail.length > 0) {
    return errDetail[0].msg
  }
  if (typeof errDetail === "string") {
    return errDetail
  }
  if (errDetail && typeof errDetail === "object") {
    return (
      errDetail.message ||
      errDetail.msg ||
      errDetail.code ||
      "Something went wrong."
    )
  }
  return "Something went wrong."
}

/**
 * Clear the per-login disclaimer acknowledgment(s).
 *
 * The "Every Login" disclaimer mode stores its acknowledgment in
 * `sessionStorage` under `disclaimer_session_v<version>`. sessionStorage
 * survives a logout→login cycle within the same browser tab, which would
 * suppress the disclaimer on the next login. Call this on every successful
 * login so "Every Login" truly re-shows after each sign-in. (The "New User
 * Only" mode lives in localStorage and is intentionally left untouched.)
 */
export const clearLoginScopedDisclaimerAck = () => {
  try {
    const store = window.sessionStorage
    const keys: string[] = []
    for (let i = 0; i < store.length; i++) {
      const key = store.key(i)
      if (key && key.startsWith("disclaimer_session_")) {
        keys.push(key)
      }
    }
    keys.forEach((key) => store.removeItem(key))
  } catch {
    // sessionStorage may be unavailable (e.g. privacy mode) — ignore.
  }
}

export const handleError = function (
  this: (msg: string) => void,
  err: ApiError,
) {
  const errorMessage = extractErrorMessage(err)
  this(errorMessage)
}

/**
 * Best-effort error-message extraction for `useMutation` `onError` handlers that
 * want a custom fallback. Accepts the loosely-typed React Query error (defaults
 * to `Error`) and prefers a FastAPI `body.detail` string, then `message`.
 *
 * `detail` is NOT always a string: the recoverable-409 endpoints (git connect's
 * `existing_agent_folder`, git pull's `local_changes`) return a structured
 * object carrying `code` / `message`. Returning that object from a function
 * typed `string` renders as `[object Object]` in a toast, so an object detail
 * falls through to its own `message` — mirroring `extractErrorMessage` above.
 */
export const getErrorMessage = (error: unknown, fallback: string): string => {
  const e = error as { body?: { detail?: unknown }; message?: string }
  const detail = e?.body?.detail
  if (typeof detail === "string" && detail) return detail
  if (detail && typeof detail === "object") {
    const nested = (detail as { message?: unknown }).message
    if (typeof nested === "string" && nested) return nested
  }
  return e?.message || fallback
}

export const getInitials = (name: string): string => {
  return name
    .split(" ")
    .slice(0, 2)
    .map((word) => word[0])
    .join("")
    .toUpperCase()
}

/**
 * Validate a post-auth `?redirect=` target. Only same-origin local paths
 * are allowed, to prevent open-redirect attacks. Returns "/" for anything
 * unsafe or empty.
 */
export const safeRedirectPath = (input: string | null | undefined): string => {
  if (!input || typeof input !== "string") return "/"
  if (!input.startsWith("/")) return "/"
  // Reject protocol-relative URLs and backslash tricks
  if (input.startsWith("//") || input.startsWith("/\\")) return "/"
  try {
    const url = new URL(input, window.location.origin)
    if (url.origin !== window.location.origin) return "/"
    return url.pathname + url.search + url.hash
  } catch {
    return "/"
  }
}

// ── The backend address, in the two shapes callers need ─────────────────────
// These are *not* interchangeable. `resolveApiBase` is what you prepend to a
// route path; `resolveServerOrigin` is what you hand to a native client. On a
// reverse-proxy deployment (`VITE_API_URL=/api`) they differ by a path segment,
// and using the wrong one fails silently — see each docstring.

/**
 * The absolute base URL the API is served from, with no trailing slash.
 *
 * For **building URLs to backend routes**: `${resolveApiBase()}/api/v1/…`
 * mirrors exactly how the generated client prepends `OpenAPI.BASE` to every
 * path it calls, so a URL built this way reaches the same place the client
 * does. Its only consumer today is the desktop download resolver
 * (`components/Desktop/DesktopLandingPage.tsx`).
 *
 * This returns a **base, which may carry a path** — it is not an origin. With
 * `VITE_API_URL=/api` it resolves to `https://app.example.com/api`. Anything
 * that needs a bare `scheme://host[:port]` must use `resolveServerOrigin`.
 *
 * Deliberately **not** bare `import.meta.env.VITE_API_URL`. That value is
 * unvalidated build input and reaches us in three shapes this normalises:
 *   - absent — `main.tsx` assigns it to `OpenAPI.BASE` with no fallback, so it
 *     can be `undefined`, or the literal string `"undefined"` if a build
 *     pipeline stringified an unset variable into it;
 *   - relative (`/api`), which is meaningful only against the page that loaded
 *     it, and which a native client cannot resolve at all;
 *   - trailing-slashed, which would produce `https://host//api/v1/…`.
 *
 * The `OpenAPI.BASE || window.location.origin` shape mirrors the existing
 * console-socket URL builder (`hooks/useEnvConsoleSocket.ts`).
 */
export const resolveApiBase = (): string => {
  const configured = OpenAPI.BASE
  // `=== "undefined"` is not redundant with `||`: it catches a *stringified*
  // unset build variable, which `||` treats as a perfectly good base. Worth
  // guarding here specifically because this value is shown to a person and
  // embedded in a deep link rather than fetched — a wrong one fails silently
  // inside the desktop app instead of surfacing as a failed request.
  const base = (
    !configured || configured === "undefined"
      ? window.location.origin
      : configured
  )
  // `\/+$`, not `\/$`: a single-slash strip leaves `https://host//` as
  // `https://host/`, which still doubles the separator downstream.
    .replace(/\/+$/, "")
  // Match the scheme, not the prefix: `startsWith("http")` would also accept
  // `httpx://…` and hand it out as if it were absolute.
  if (/^https?:\/\//i.test(base)) return base
  // Relative (`/api`, or a bare `api`): resolve against the page it was served
  // from. An unrecognised scheme lands here too and produces a visibly broken
  // URL, which beats silently passing garbage off as an absolute address.
  return `${window.location.origin}${base.startsWith("/") ? "" : "/"}${base}`
}

/**
 * This instance's server **origin** — `scheme://host[:port]`, no path.
 *
 * For **handing the backend address to Cinna Desktop**: the
 * `cinna://connect?server=…` deep link and the paste-fallback address a person
 * types into the app. The desktop resolves `<server>/.well-known/cinna-desktop`,
 * which FastAPI mounts at the **app root** rather than under the API base path,
 * so any path on the configured base must be dropped here. With
 * `VITE_API_URL=/api`, handing over `resolveApiBase()` would have the desktop
 * discover against `https://app.example.com/api/.well-known/cinna-desktop` and
 * 404 — while downloads on the same page kept working, so nothing looked wrong.
 *
 * Deliberately **not** `window.location.origin`: on a split-host deployment the
 * SPA is served from `app.example.com`, which has no backend at all, so the
 * desktop would discover against the wrong host and fail with nothing to see.
 * (Contrast `localAgentKitStartUrl`, which *does* want the page origin because
 * `/agent-start` is a frontend proxy path.)
 */
export const resolveServerOrigin = (): string => {
  const base = resolveApiBase()
  try {
    return new URL(base).origin
  } catch {
    // `base` is absolute by construction, so this needs a malformed
    // `VITE_API_URL` to reach. Falling back keeps the page rendering and shows
    // a wrong-but-visible address, instead of throwing inside a `useMemo`.
    return window.location.origin
  }
}

/**
 * The deep link that hands Cinna Desktop this instance's server address.
 *
 * Uses the origin, not the API base: this value is the desktop's discovery
 * root. It must stay identical to the paste-fallback address the landing page
 * displays, so that copying it by hand reaches the same server the deep link
 * would have.
 *
 * `URLSearchParams` percent-encodes the `:` and `//` of the origin, so the
 * `server` value survives a custom-scheme handler that parses the query with a
 * standard URL parser. This string is a cross-repo contract with cinna-desktop
 * — change it here and in the desktop's handler together.
 */
export const cinnaConnectDeepLink = (): string =>
  `cinna://connect?${new URLSearchParams({ server: resolveServerOrigin() })}`

// ── Authenticated binary downloads ──────────────────────────────────────────
// The generated OpenAPI client is awkward with binary responses (it types them
// as `unknown` and does not always set `responseType: "blob"`), so raw-file
// endpoints are fetched by hand with the bearer token the client would have
// attached. One implementation, used by the chat attachment components and the
// improvement-request archive download alike.

// Falls back to "" (same origin), matching `OpenAPI.BASE` in `main.tsx` and the
// other hand-rolled API callers. A `localhost:8000` fallback would silently
// misdirect these requests on a same-origin deployment where VITE_API_URL is
// unset — and there the generated client would be working fine, which makes
// that failure mode particularly hard to spot.
const API_BASE_URL = import.meta.env.VITE_API_URL || ""

/**
 * GET an API path with the stored access token. `path` is relative to the API
 * origin and must include the `/api/v1` prefix, e.g.
 * `/api/v1/files/{id}/download`.
 *
 * Returns the whole `Response`, for the callers that need a header off it —
 * `Content-Disposition`, typically, when the server names the file. Prefer
 * `fetchAuthenticatedBlob` when the body is all you want.
 *
 * Throws on a non-2xx response so callers can surface a toast.
 */
export const fetchAuthenticatedResponse = async (
  path: string,
): Promise<Response> => {
  const token = localStorage.getItem("access_token")
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!response.ok) {
    throw new Error(`Download failed (${response.status})`)
  }
  return response
}

/** Authenticated GET returning just the response body as a Blob. */
export const fetchAuthenticatedBlob = async (path: string): Promise<Blob> =>
  (await fetchAuthenticatedResponse(path)).blob()

/**
 * The filename a server named in `Content-Disposition`, or `fallback` when the
 * header is absent or unparseable.
 */
export const filenameFromResponse = (
  response: Response,
  fallback: string,
): string => {
  const disposition = response.headers.get("content-disposition") || ""
  return disposition.match(/filename="?([^"]+)"?/)?.[1] || fallback
}

/** Hand a Blob to the browser as a file save, via a temporary object URL. */
export const saveBlobAs = (blob: Blob, filename: string): void => {
  const url = window.URL.createObjectURL(blob)
  const anchor = document.createElement("a")
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  window.URL.revokeObjectURL(url)
}

/** Fetch an authenticated binary endpoint and save it under `filename`. */
export const downloadAuthenticatedFile = async (
  path: string,
  filename: string,
): Promise<void> => {
  saveBlobAs(await fetchAuthenticatedBlob(path), filename)
}
