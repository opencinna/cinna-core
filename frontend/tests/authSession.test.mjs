// Token store (src/lib/authSession.ts) and login throttle copy
// (src/utils/loginThrottle.ts). Run: node --test tests/authSession.test.mjs
import assert from "node:assert/strict"
import { beforeEach, test } from "node:test"

class MemoryStorage {
  #items = new Map()
  getItem(key) {
    return this.#items.has(key) ? this.#items.get(key) : null
  }
  setItem(key, value) {
    this.#items.set(key, String(value))
  }
  removeItem(key) {
    this.#items.delete(key)
  }
  clear() {
    this.#items.clear()
  }
}

globalThis.localStorage = new MemoryStorage()
globalThis.sessionStorage = new MemoryStorage()

const session = await import("../src/lib/authSession.ts")
const throttle = await import("../src/utils/loginThrottle.ts")

/** An unsigned JWT-shaped token expiring `seconds` from now. */
function jwt(seconds, extra = {}) {
  const encode = (value) =>
    Buffer.from(JSON.stringify(value)).toString("base64url")
  const exp = Math.floor(Date.now() / 1000) + seconds
  return `${encode({ alg: "HS256" })}.${encode({ sub: "u", exp, ...extra })}.sig`
}

function fakeTransport(script) {
  const calls = { refresh: [], logout: [] }
  return {
    calls,
    transport: {
      refresh: async (csrf) => {
        calls.refresh.push(csrf)
        return script.refresh(csrf)
      },
      logout: async (csrf) => {
        calls.logout.push(csrf)
        return script.logout?.(csrf)
      },
    },
  }
}

beforeEach(() => {
  session.clearSession()
  session.clearGuestToken()
  localStorage.clear()
  sessionStorage.clear()
})

test("no user JWT is written to Web Storage; only the CSRF token persists", () => {
  const token = jwt(900)
  session.setSession(token, "csrf-1")
  assert.equal(session.getAccessToken(), token)
  assert.equal(localStorage.getItem("cinna_csrf"), "csrf-1")
  assert.equal(localStorage.getItem("access_token"), null)
  assert.equal(sessionStorage.getItem("access_token"), null)
})

test("bootstrap removes a legacy localStorage access_token", async () => {
  localStorage.setItem("access_token", "legacy")
  session.configureAuthSession(
    fakeTransport({ refresh: () => assert.fail("no csrf, no refresh") })
      .transport,
  )
  await session.bootstrapSession()
  assert.equal(localStorage.getItem("access_token"), null)
  assert.equal(session.getAccessToken(), null)
})

test("bootstrap restores the session from the cookie when cinna_csrf exists", async () => {
  const fresh = jwt(900)
  localStorage.setItem("cinna_csrf", "csrf-2")
  const fake = fakeTransport({ refresh: () => fresh })
  session.configureAuthSession({ transport: fake.transport })
  await session.bootstrapSession()
  assert.deepEqual(fake.calls.refresh, ["csrf-2"])
  assert.equal(session.getAccessToken(), fresh)
})

test("bootstrap 401 session_expired clears cinna_csrf", async () => {
  localStorage.setItem("cinna_csrf", "csrf-3")
  const fake = fakeTransport({
    refresh: () => Promise.reject({ status: 401 }),
  })
  session.configureAuthSession({ transport: fake.transport })
  await session.bootstrapSession()
  assert.equal(session.getAccessToken(), null)
  assert.equal(localStorage.getItem("cinna_csrf"), null)
})

test("bootstrap network error keeps cinna_csrf so the next reload retries", async () => {
  localStorage.setItem("cinna_csrf", "csrf-4")
  const fake = fakeTransport({
    refresh: () => Promise.reject(new Error("Network Error")),
  })
  session.configureAuthSession({ transport: fake.transport })
  await session.bootstrapSession()
  assert.equal(session.getAccessToken(), null)
  assert.equal(localStorage.getItem("cinna_csrf"), "csrf-4")
})

test("bootstrap is capped: a hung refresh renders signed out, csrf kept", async () => {
  localStorage.setItem("cinna_csrf", "csrf-5")
  let release
  const fake = fakeTransport({
    refresh: () =>
      new Promise((resolve) => {
        release = resolve
      }),
  })
  session.configureAuthSession({ transport: fake.transport })
  const started = Date.now()
  await session.bootstrapSession(50)
  assert.ok(Date.now() - started < 1000)
  assert.equal(session.getAccessToken(), null)
  assert.equal(localStorage.getItem("cinna_csrf"), "csrf-5")
  release(jwt(900))
  await session.refreshSession()
})

test("getFreshAccessToken refreshes only within 30 s of exp", async () => {
  const fresh = jwt(900)
  const fake = fakeTransport({ refresh: () => fresh })
  session.configureAuthSession({ transport: fake.transport })

  const longLived = jwt(600)
  session.setSession(longLived, "csrf-6")
  assert.equal(await session.getFreshAccessToken(), longLived)
  assert.equal(fake.calls.refresh.length, 0)

  session.setSession(jwt(10), "csrf-6")
  assert.equal(await session.getFreshAccessToken(), fresh)
  assert.equal(fake.calls.refresh.length, 1)
})

test("concurrent refreshes in one tab share a single request", async () => {
  const fresh = jwt(900)
  let resolveRefresh
  const fake = fakeTransport({
    refresh: () =>
      new Promise((resolve) => {
        resolveRefresh = resolve
      }),
  })
  session.configureAuthSession({ transport: fake.transport })
  session.setSession(jwt(5), "csrf-7")
  const all = Promise.all([
    session.getFreshAccessToken(),
    session.getFreshAccessToken(),
    session.refreshSession(),
  ])
  await new Promise((r) => setTimeout(r, 10))
  resolveRefresh(fresh)
  const [a, b, c] = await all
  assert.equal(fake.calls.refresh.length, 1)
  assert.equal(a, fresh)
  assert.equal(b, fresh)
  assert.deepEqual(c, { kind: "ok", token: fresh })
})

test("refreshAfterRejection reuses a token that already replaced the rejected one", async () => {
  const fake = fakeTransport({ refresh: () => assert.fail("must not refresh") })
  session.configureAuthSession({ transport: fake.transport })
  const current = jwt(900)
  session.setSession(current, "csrf-8")
  const outcome = await session.refreshAfterRejection("an-older-token")
  assert.deepEqual(outcome, { kind: "ok", token: current })
})

test("refreshAfterRejection refreshes a stale-but-unexpired token (password change)", async () => {
  const stale = jwt(800, { sv: 1 })
  const fresh = jwt(900, { sv: 2 })
  const fake = fakeTransport({ refresh: () => fresh })
  session.configureAuthSession({ transport: fake.transport })
  session.setSession(stale, "csrf-9")
  const outcome = await session.refreshAfterRejection(stale)
  assert.deepEqual(outcome, { kind: "ok", token: fresh })
  assert.equal(session.getAccessToken(), fresh)
})

test("a refresh 403 csrf_failed is inconclusive: nothing is cleared", async () => {
  const fake = fakeTransport({ refresh: () => Promise.reject({ status: 403 }) })
  session.configureAuthSession({ transport: fake.transport })
  const token = jwt(5)
  session.setSession(token, "csrf-10")
  assert.deepEqual(await session.refreshSession(), { kind: "failed" })
  assert.equal(localStorage.getItem("cinna_csrf"), "csrf-10")
  assert.equal(await session.getFreshAccessToken(), token)
})

test("a transient refresh failure marks the next 401 inconclusive; only expiry ends it", async () => {
  let respond = () => Promise.reject({ status: 503 })
  const fake = fakeTransport({ refresh: () => respond() })
  session.configureAuthSession({ transport: fake.transport })
  const token = jwt(5)
  session.setSession(token, "csrf-10b")
  assert.equal(session.isRefreshInconclusive(), false)

  // 5xx: the cookie session may be alive — a page 401 handler must not clear.
  assert.deepEqual(await session.refreshAfterRejection(token), {
    kind: "failed",
  })
  assert.equal(session.isRefreshInconclusive(), true)
  assert.equal(localStorage.getItem("cinna_csrf"), "csrf-10b")

  respond = () => Promise.reject(new Error("Network Error"))
  await session.refreshAfterRejection(token)
  assert.equal(session.isRefreshInconclusive(), true)

  // A later success settles it.
  const fresh = jwt(900)
  respond = () => fresh
  assert.deepEqual(await session.refreshAfterRejection(token), {
    kind: "ok",
    token: fresh,
  })
  assert.equal(session.isRefreshInconclusive(), false)

  // 401 session_expired: conclusive, and the store has already cleared.
  respond = () => Promise.reject({ status: 401 })
  assert.deepEqual(await session.refreshAfterRejection(fresh), {
    kind: "expired",
  })
  assert.equal(session.isRefreshInconclusive(), false)
  assert.equal(localStorage.getItem("cinna_csrf"), null)
})

test("logout clears locally even when the server call fails, and broadcasts", async () => {
  const fake = fakeTransport({
    refresh: () => jwt(900),
    logout: () => Promise.reject(new Error("offline")),
  })
  session.configureAuthSession({ transport: fake.transport })
  const otherTab = new BroadcastChannel("cinna-auth")
  const received = new Promise((resolve) => {
    otherTab.onmessage = (event) => resolve(event.data)
  })
  session.setSession(jwt(900), "csrf-11")
  await session.logoutSession()
  assert.deepEqual(fake.calls.logout, ["csrf-11"])
  assert.equal(session.getAccessToken(), null)
  assert.equal(localStorage.getItem("cinna_csrf"), null)
  assert.deepEqual(await received, { type: "logout" })
  otherTab.close()
})

test("logout waits at most LOGOUT_WAIT_MS for a hung server", async () => {
  const fake = fakeTransport({
    refresh: () => jwt(900),
    logout: () => new Promise(() => {}),
  })
  session.configureAuthSession({ transport: fake.transport })
  session.setSession(jwt(900), "csrf-12")
  const started = Date.now()
  await session.logoutSession(30)
  assert.ok(Date.now() - started < 1000)
  assert.equal(session.getAccessToken(), null)
})

test("another tab's logout clears this tab and runs the handler", async () => {
  let loggedOut = false
  session.configureAuthSession({
    transport: fakeTransport({ refresh: () => jwt(900) }).transport,
    onRemoteLogout: () => {
      loggedOut = true
    },
  })
  session.setSession(jwt(900), "csrf-13")
  const otherTab = new BroadcastChannel("cinna-auth")
  otherTab.postMessage({ type: "logout" })
  await new Promise((r) => setTimeout(r, 50))
  otherTab.close()
  assert.equal(session.getAccessToken(), null)
  assert.equal(loggedOut, true)
})

test("another tab's refreshed token is adopted only by a signed-in tab", async () => {
  session.configureAuthSession({
    transport: fakeTransport({ refresh: () => jwt(900) }).transport,
  })
  const otherTab = new BroadcastChannel("cinna-auth")
  const broadcastToken = jwt(900, { n: 1 })
  otherTab.postMessage({ type: "token", token: broadcastToken })
  await new Promise((r) => setTimeout(r, 50))
  assert.equal(session.getAccessToken(), null)

  session.setSession(jwt(900), "csrf-14")
  otherTab.postMessage({ type: "token", token: broadcastToken })
  await new Promise((r) => setTimeout(r, 50))
  otherTab.close()
  assert.equal(session.getAccessToken(), broadcastToken)
})

test("the guest token never replaces a user session and stays in sessionStorage", async () => {
  session.configureAuthSession({
    transport: fakeTransport({ refresh: () => jwt(900) }).transport,
  })
  const guest = jwt(3600, { kind: "guest" })
  session.setGuestToken(guest)
  assert.equal(sessionStorage.getItem("guest_access_token"), guest)
  assert.equal(localStorage.getItem("access_token"), null)
  assert.equal(await session.resolveBearer(), guest)

  const user = jwt(900)
  session.setSession(user, "csrf-15")
  assert.equal(await session.resolveBearer(), user)
  assert.equal(session.getGuestToken(), guest)
})

test("login throttle copy follows Retry-After", () => {
  const m = throttle.loginThrottleMessage
  assert.equal(m(1), "Too many sign-in attempts. Try again in 1 second.")
  assert.equal(m(45), "Too many sign-in attempts. Try again in 45 seconds.")
  assert.equal(m(60), "Too many sign-in attempts. Try again in 1 minute.")
  assert.equal(m(61), "Too many sign-in attempts. Try again in 2 minutes.")
  assert.equal(m(900), "Too many sign-in attempts. Try again in 15 minutes.")
  assert.equal(
    m(null),
    "Too many sign-in attempts. Try again in a few minutes.",
  )
})

test("rate-limited 429 is recognised and Retry-After read via the body", () => {
  const body = { detail: { code: "rate_limited", message: "rate_limited" } }
  throttle.recordRetryAfter(body, "42")
  const error = { status: 429, body }
  assert.equal(throttle.isLoginRateLimited(error), true)
  assert.equal(throttle.retryAfterSeconds(error), 42)

  const noHeader = { status: 429, body: { detail: { code: "rate_limited" } } }
  assert.equal(throttle.retryAfterSeconds(noHeader), null)
  assert.equal(throttle.parseRetryAfter("Wed, 21 Oct 2026 07:28:00 GMT"), null)
  assert.equal(
    throttle.isLoginRateLimited({
      status: 429,
      body: { detail: "Rate limit exceeded" },
    }),
    false,
  )
  assert.equal(throttle.isLoginRateLimited({ status: 400, body }), false)
})
