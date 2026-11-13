import assert from "node:assert/strict"
import path from "node:path"
import { after, before, test } from "node:test"
import { fileURLToPath } from "node:url"
import { chromium, expect } from "@playwright/test"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react-swc"
import { createServer } from "vite"

const frontendRoot = fileURLToPath(new URL("..", import.meta.url))
const CREDENTIAL_ID = "3f2b8c1e-5a4d-4e8f-9b7a-0c1d2e3f4a5b"
const SUCCESS_LINK = `cinna://oauth-done?credential_id=${CREDENTIAL_ID}`
const ERROR_LINK = "cinna://oauth-done?status=error"

let server
let baseUrl
let harnessHtml
let browser

before(async () => {
  server = await createServer({
    configFile: false,
    root: frontendRoot,
    resolve: { alias: { "@": path.join(frontendRoot, "src") } },
    plugins: [react(), tailwindcss()],
    server: { host: "127.0.0.1", port: 0 },
    logLevel: "error",
  })
  await server.listen()
  baseUrl = `http://127.0.0.1:${server.httpServer.address().port}`
  harnessHtml = await server.transformIndexHtml(
    "/",
    '<html><body><div id="root"></div><script type="module" src="/tests/fixtures/oauthCallbackHarness.tsx"></script></body></html>',
  )
  browser = await chromium.launch({ headless: true })
})

after(async () => {
  await browser?.close()
  await server?.close()
})

/**
 * Opens the callback page at `query`, with the callback POST answered by
 * `callback` ({status, body}). Every `cinna://` navigation the page starts is
 * recorded (and goes nowhere: the scheme has no handler in the test browser).
 */
async function openCallback(t, query, callback) {
  const context = await browser.newContext()
  t.after(() => context.close())
  const page = await context.newPage()
  page.setDefaultTimeout(10000)
  const errors = []
  page.on("pageerror", (error) => errors.push(error.message))
  t.after(() => assert.deepEqual(errors, []))

  const cdp = await context.newCDPSession(page)
  await cdp.send("Page.enable")
  const deepLinks = []
  cdp.on("Page.frameRequestedNavigation", ({ url }) => {
    if (url.startsWith("cinna:")) deepLinks.push(url)
  })

  const posts = []
  await page.route("**/api/v1/credentials/oauth/callback", (route) => {
    posts.push(route.request().postDataJSON())
    return route.fulfill({
      status: callback.status,
      contentType: "application/json",
      body: JSON.stringify(callback.body),
    })
  })
  await page.route(`${baseUrl}/credentials/oauth/callback**`, (route) =>
    route.fulfill({ contentType: "text/html", body: harnessHtml }),
  )
  await page.goto(`${baseUrl}/credentials/oauth/callback?${query}`)
  return { page, deepLinks, posts }
}

const openDesktop = (page) =>
  page.getByRole("link", { name: "Open Cinna Desktop" })

test("desktop success opens the app once and offers the link", async (t) => {
  const { page, deepLinks, posts } = await openCallback(
    t,
    "code=abc&state=desktop.xyz",
    {
      status: 200,
      body: {
        success: true,
        credential_id: CREDENTIAL_ID,
        origin: "desktop",
        owner_email_masked: "j***@example.com",
      },
    },
  )
  await expect(page.getByText("Connected", { exact: true })).toBeVisible()
  await expect(openDesktop(page)).toHaveAttribute("href", SUCCESS_LINK)
  await expect(
    page.getByText("You can close this tab.", { exact: true }),
  ).toBeVisible()
  await expect.poll(() => deepLinks).toEqual([SUCCESS_LINK])
  // Re-renders after success neither re-POST the code nor repeat the hand-back.
  await page.waitForTimeout(500)
  assert.equal(posts.length, 1)
  assert.deepEqual(deepLinks, [SUCCESS_LINK])
})

test("Google cancellation offers the error link without navigating", async (t) => {
  const { page, deepLinks, posts } = await openCallback(
    t,
    "error=access_denied&state=desktop.xyz",
    { status: 500, body: {} },
  )
  await expect(page.getByText("Authorization cancelled")).toBeVisible()
  await expect(page.getByText("Google access was not granted.")).toBeVisible()
  await expect(openDesktop(page)).toHaveAttribute("href", ERROR_LINK)
  await page.waitForTimeout(500)
  assert.deepEqual(deepLinks, [])
  assert.equal(posts.length, 0)
})

test("an incomplete desktop link offers the error link", async (t) => {
  const { page, deepLinks } = await openCallback(t, "state=desktop.xyz", {
    status: 500,
    body: {},
  })
  await expect(
    page.getByText("The authorization link is incomplete."),
  ).toBeVisible()
  await expect(openDesktop(page)).toHaveAttribute("href", ERROR_LINK)
  await page.waitForTimeout(500)
  assert.deepEqual(deepLinks, [])
})

test("a failed desktop callback offers the error link without navigating", async (t) => {
  const { page, deepLinks } = await openCallback(
    t,
    "code=abc&state=desktop.xyz",
    { status: 400, body: { detail: "Invalid or expired state" } },
  )
  await expect(page.getByText("Authorization failed")).toBeVisible()
  await expect(page.getByText("Invalid or expired state")).toBeVisible()
  await expect(openDesktop(page)).toHaveAttribute("href", ERROR_LINK)
  await page.waitForTimeout(500)
  assert.deepEqual(deepLinks, [])
})

test("web success neither opens the app nor offers the link", async (t) => {
  const { page, deepLinks, posts } = await openCallback(
    t,
    "code=abc&state=web-xyz",
    {
      status: 200,
      body: { success: true, credential_id: CREDENTIAL_ID, origin: "web" },
    },
  )
  await expect.poll(() => posts.length).toBe(1)
  await page.waitForTimeout(500)
  assert.deepEqual(deepLinks, [])
  await expect(openDesktop(page)).toHaveCount(0)
})
