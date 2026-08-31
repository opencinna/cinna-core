import type { AxiosResponse } from "axios"

import { ApiError, OpenAPI } from "@/client"

/**
 * Turning the routing endpoints' 429 into an honest "try again in N seconds".
 *
 * `POST /admin/routing/simulate`, `/replay` and `/recommendation` share one
 * per-admin bucket and answer 429 with a `Retry-After` header. The generated
 * client's `ApiError` carries `status` and `body` but **not** response headers
 * (`ApiResult` has no `headers` field), so the number is not reachable from a
 * mutation's `onError` on its own.
 *
 * Rather than edit the generated client — it is regenerated, never edited — this
 * uses the response-interceptor hook the generated `OpenAPI` config already
 * exposes. `request.ts` runs those interceptors on the error response too
 * (`sendRequest` returns `axiosError.response`), so a 429 passes through here
 * before `ApiError` is constructed.
 *
 * **The countdown is never invented.** If the header is missing or unparseable,
 * `retryAfterSeconds` returns `null` and the caller renders the rate-limit
 * message *without* a number. A fabricated "try again in 30s" would be exactly
 * the class of lie this card exists to remove.
 */

const ROUTING_PATH = "/admin/routing/"

/** Epoch ms at which the throttled admin may retry, or `null` if unknown. */
let retryAt: number | null = null
let installed = false

function captureRetryAfter(response: AxiosResponse): AxiosResponse {
  try {
    if (response.status !== 429) return response
    const url = String(response.config?.url ?? "")
    if (!url.includes(ROUTING_PATH)) return response
    // Axios lowercases response header names.
    const raw = response.headers?.["retry-after"]
    const seconds = Number.parseInt(String(raw ?? ""), 10)
    retryAt =
      Number.isFinite(seconds) && seconds >= 0 ? Date.now() + seconds * 1000 : null
  } catch {
    // A diagnostic aid must never break the request it observes (plan §11a
    // Rule 2). Losing the countdown is recoverable; throwing here would turn a
    // 429 into an unhandled rejection.
    retryAt = null
  }
  return response
}

/**
 * Idempotent. Called from the card's mount effect rather than at import time so
 * the interceptor's lifetime is tied to something visible.
 */
export function installRoutingRateLimitCapture(): void {
  if (installed) return
  OpenAPI.interceptors.response.use(captureRetryAfter)
  installed = true
}

/**
 * Whole seconds left on the throttle, or `null` when the header was absent.
 *
 * Read imperatively, and module-global — so a `RateLimited` that is already
 * mounted will not pick up a *newer* 429's number, and two panels showing a 429
 * at once share one value. Neither is reachable today: a retry sets
 * `isPending`, which unmounts the error branch before the next response lands.
 * That ordering lives in `RoutingStateBlocks.tsx`, not here, so it is written
 * down in both places — a load-bearing dependency on another file's render
 * order is exactly the kind of condition that otherwise goes unrecorded.
 */
export function retryAfterSeconds(): number | null {
  if (retryAt === null) return null
  const remaining = Math.ceil((retryAt - Date.now()) / 1000)
  return remaining > 0 ? remaining : 0
}

export function isRateLimited(error: unknown): boolean {
  return error instanceof ApiError && error.status === 429
}
