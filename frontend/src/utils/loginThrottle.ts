/**
 * The login throttle (429 `{"detail":{"code":"rate_limited",...}}` with a
 * `Retry-After` header in integer seconds) as the login page shows it.
 *
 * The generated `ApiError` carries no headers, so the response interceptor
 * records `Retry-After` here, keyed by the response body object, which is the
 * very object the client later puts on `ApiError.body`. Pure module (no
 * generated-client import) so it runs under `node --test`.
 */

const retryAfterByBody = new WeakMap<object, number>()

/** Parse `Retry-After` as integer seconds; null when missing or unparsable. */
export function parseRetryAfter(header: unknown): number | null {
  if (typeof header !== "string" || !/^\s*\d+\s*$/.test(header)) return null
  const seconds = Number.parseInt(header, 10)
  return Number.isFinite(seconds) ? seconds : null
}

/** Called by the response interceptor for every 429. */
export function recordRetryAfter(body: unknown, header: unknown): void {
  const seconds = parseRetryAfter(header)
  if (body && typeof body === "object" && seconds !== null) {
    retryAfterByBody.set(body, seconds)
  }
}

function bodyOf(error: unknown): unknown {
  return error && typeof error === "object" && "body" in error
    ? error.body
    : undefined
}

/** True for the login throttle's 429, whose `message` is a code, not copy. */
export function isLoginRateLimited(error: unknown): boolean {
  if (!error || typeof error !== "object" || !("status" in error)) return false
  if (error.status !== 429) return false
  const body = bodyOf(error)
  if (!body || typeof body !== "object" || !("detail" in body)) return false
  const detail = body.detail
  return (
    !!detail &&
    typeof detail === "object" &&
    "code" in detail &&
    detail.code === "rate_limited"
  )
}

/** Seconds from the 429's `Retry-After`, or null when it wasn't readable. */
export function retryAfterSeconds(error: unknown): number | null {
  const body = bodyOf(error)
  if (!body || typeof body !== "object") return null
  return retryAfterByBody.get(body) ?? null
}

/** S1 copy. Same for known and unknown accounts: never mention the email. */
export function loginThrottleMessage(seconds: number | null): string {
  if (seconds === null) {
    return "Too many sign-in attempts. Try again in a few minutes."
  }
  const s = Math.max(1, seconds)
  if (s < 60) {
    return `Too many sign-in attempts. Try again in ${s} ${s === 1 ? "second" : "seconds"}.`
  }
  const m = Math.ceil(s / 60)
  return `Too many sign-in attempts. Try again in ${m} ${m === 1 ? "minute" : "minutes"}.`
}
