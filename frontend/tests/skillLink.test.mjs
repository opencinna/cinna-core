// Skill links: the public /get/skill/<package_id> page and Copy install link.
//
// Runs the production `SkillLinkPage` and `useCopySkillInstallLink` in
// Chromium through a Vite harness; every API call is answered here.
import assert from "node:assert/strict"
import path from "node:path"
import { after, before, test } from "node:test"
import { fileURLToPath } from "node:url"
import { chromium, expect } from "@playwright/test"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react-swc"
import { createServer } from "vite"

const frontendRoot = fileURLToPath(new URL("..", import.meta.url))
const PACKAGE_ID = "io.example.team.skill.pdf-report"
const PACKAGE_UUID = "6f1c2a5e-0000-4000-8000-000000000001"
const USER_AGENTS = {
  darwin:
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
  linux:
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
}
const PACKAGE = {
  id: PACKAGE_UUID,
  package_id: PACKAGE_ID,
  name: "pdf-report",
  display_name: "PDF Report",
  description: "Turns a table into a printable PDF report.",
  publisher_user_id: "00000000-0000-4000-8000-000000000002",
  publisher_name: "Ada Publisher",
  publisher_email: "ada@example.com",
  visibility: "public",
  is_listed: true,
  latest_revision_id: "rev-3",
  latest_revision_number: 3,
  latest_version: "1.2.0",
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-01T00:00:00Z",
  revisions: [
    {
      id: "rev-3",
      revision_number: 3,
      version: "1.2.0",
      required_credentials: [
        { slot: "erp_api", type: "api_token", provided_by: "user" },
      ],
    },
    {
      id: "rev-2",
      revision_number: 2,
      version: "1.1.0",
      required_credentials: [],
    },
  ],
}

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
    '<html><body><div id="root"></div><script type="module" src="/tests/fixtures/skillLinkHarness.tsx"></script></body></html>',
  )
  browser = await chromium.launch({ headless: true })
})

after(async () => {
  await browser?.close()
  await server?.close()
})

/**
 * Opens `pathname` as a visitor on `platform`, signed in when `token` is set,
 * with the package lookup answered by `lookup` (a status, or 200 + PACKAGE).
 */
async function open(
  t,
  pathname,
  { platform = "darwin", token = null, lookup = 200 } = {},
) {
  const context = await browser.newContext({
    userAgent: USER_AGENTS[platform],
  })
  t.after(() => context.close())
  const page = await context.newPage()
  page.setDefaultTimeout(10000)
  const errors = []
  page.on("pageerror", (error) => errors.push(error.message))
  t.after(() => assert.deepEqual(errors, []))

  await page.addInitScript(
    ({ token }) => {
      if (token) localStorage.setItem("access_token", token)
      window.__clipboardWrites = []
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText(text) {
            window.__clipboardWrites.push(text)
            return Promise.resolve()
          },
        },
      })
    },
    { token },
  )
  const html = (route) =>
    route.fulfill({ contentType: "text/html", body: harnessHtml })
  await page.route(`${baseUrl}/get/**`, html)
  await page.route(`${baseUrl}/copy/**`, html)
  const calls = []
  await page.route("**/api/v1/**", (route) => {
    const url = new URL(route.request().url())
    calls.push({
      path: url.pathname,
      auth: route.request().headers().authorization ?? null,
    })
    if (lookup === 200) {
      return route.fulfill({ status: 200, json: PACKAGE })
    }
    return route.fulfill({
      status: lookup,
      json: {
        detail: {
          code: "package_not_found",
          message: "Skill package not found",
        },
      },
    })
  })
  await page.goto(`${baseUrl}${pathname}`)
  return { page, calls }
}

/** The card's text: what a visitor can read, compared across states. */
async function cardText(page) {
  await expect(
    page.getByText("Someone shared a Cinna skill with you"),
  ).toBeVisible()
  return page.locator('[data-slot="card"]').innerText()
}

test("signed out: neutral page, exact cinna:// href, no lookup", async (t) => {
  const { page, calls } = await open(t, `/get/skill/${PACKAGE_ID}`)

  await expect(
    page.getByText("Someone shared a Cinna skill with you"),
  ).toBeVisible()
  await expect(page.getByText(PACKAGE_ID, { exact: true })).toBeVisible()
  await expect(page.getByText("127.0.0.1", { exact: false })).toBeVisible()

  const open_ = page.getByRole("link", { name: "Open in Cinna Desktop" })
  assert.equal(
    await open_.getAttribute("href"),
    `cinna://skill/${PACKAGE_ID}?server=${baseUrl}`,
  )
  await expect(
    page.getByText("or paste this page's link into Cinna Desktop"),
  ).toBeVisible()
  assert.equal(
    await page
      .getByRole("link", { name: "Get Cinna Desktop" })
      .getAttribute("href"),
    "/desktop",
  )
  await expect(page.getByRole("region", { name: "Skill details" })).toHaveCount(
    0,
  )
  assert.deepEqual(calls, [], "a signed-out visitor triggers no lookup")
})

test("signed in and visible: details and View in catalog", async (t) => {
  const { page, calls } = await open(t, `/get/skill/${PACKAGE_ID}`, {
    token: "tok",
  })

  const details = page.getByRole("region", { name: "Skill details" })
  await expect(
    details.getByRole("heading", { name: "PDF Report" }),
  ).toBeVisible()
  await expect(
    details.getByText("by Ada Publisher · Version 3 (v1.2.0)"),
  ).toBeVisible()
  await expect(
    details.getByText("Turns a table into a printable PDF report."),
  ).toBeVisible()
  await expect(details.getByText("erp_api")).toBeVisible()
  assert.equal(
    await details
      .getByRole("link", { name: "View in catalog" })
      .getAttribute("href"),
    `/catalog/skills/${PACKAGE_UUID}`,
  )
  // The link and the button stay what the signed-out page offers.
  assert.equal(
    await page
      .getByRole("link", { name: "Open in Cinna Desktop" })
      .getAttribute("href"),
    `cinna://skill/${PACKAGE_ID}?server=${baseUrl}`,
  )
  assert.deepEqual(calls, [
    {
      path: `/api/v1/skills/packages/by-package-id/${PACKAGE_ID}`,
      auth: "Bearer tok",
    },
  ])
})

test("signed in and not visible: identical to signed out", async (t) => {
  const signedOut = await open(t, `/get/skill/${PACKAGE_ID}`)
  const outText = await cardText(signedOut.page)

  const hidden = await open(t, `/get/skill/${PACKAGE_ID}`, {
    token: "tok",
    lookup: 404,
  })
  // Wait until the lookup has answered before reading the page.
  await expect.poll(() => hidden.calls.length).toBe(1)
  await hidden.page.waitForLoadState("networkidle")
  assert.equal(await cardText(hidden.page), outText)
  assert.equal(
    await hidden.page
      .getByRole("link", { name: "Open in Cinna Desktop" })
      .getAttribute("href"),
    `cinna://skill/${PACKAGE_ID}?server=${baseUrl}`,
  )
  // No error toast either: a refusal must not read differently from nothing.
  await expect(hidden.page.getByText("Something went wrong!")).toHaveCount(0)
})

test("the page is the same for any well-formed id", async (t) => {
  const a = await open(t, `/get/skill/${PACKAGE_ID}`)
  const b = await open(t, "/get/skill/io.example.private.skill.secret")
  const normalise = (text, id) => text.replace(id, "<id>")
  assert.equal(
    normalise(await cardText(a.page), PACKAGE_ID),
    normalise(await cardText(b.page), "io.example.private.skill.secret"),
  )
})

test("malformed id: the generic invalid-link page, no lookup", async (t) => {
  for (const bad of ["bad%20id", "a~b", "x".repeat(256)]) {
    const { page, calls } = await open(t, `/get/skill/${bad}`, { token: "tok" })
    await expect(page.getByText("This link isn't valid")).toBeVisible()
    await expect(
      page.getByRole("link", { name: "Open in Cinna Desktop" }),
    ).toHaveCount(0)
    assert.deepEqual(calls, [], `no lookup for ${bad}`)
  }
})

test("the paste hint is macOS-only", async (t) => {
  const { page } = await open(t, `/get/skill/${PACKAGE_ID}`, {
    platform: "linux",
  })
  await expect(
    page.getByRole("link", { name: "Open in Cinna Desktop" }),
  ).toBeVisible()
  await expect(
    page.getByText("or paste this page's link into Cinna Desktop"),
  ).toHaveCount(0)
})

test("Copy install link copies <origin>/get/skill/<package_id>", async (t) => {
  const { page } = await open(t, `/copy/${PACKAGE_ID}`)
  await page.getByRole("button", { name: "Copy install link" }).click()
  await expect(page.getByText("Install link copied")).toBeVisible()
  assert.deepEqual(await page.evaluate(() => window.__clipboardWrites), [
    `${baseUrl}/get/skill/${PACKAGE_ID}`,
  ])
})
