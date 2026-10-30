import assert from "node:assert/strict"
import path from "node:path"
import { after, before, test } from "node:test"
import { fileURLToPath } from "node:url"
import { chromium, expect, firefox, webkit } from "@playwright/test"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react-swc"
import { createServer } from "vite"

const frontendRoot = fileURLToPath(new URL("..", import.meta.url))
const NOTE =
  "Setup link copied. Paste it into the Server URL field in Cinna Desktop."
// Linux reads the clipboard on first launch, so its note asks for no paste.
const LINUX_NOTE =
  "Setup link copied. Cinna Desktop will offer to connect to this server the first time it opens."
const USER_AGENTS = {
  darwin:
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
  linux:
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
  unknown:
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
}

let server
let baseUrl
let harnessHtml
const browsers = {}

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
    '<html><body><div id="root"></div><script type="module" src="/tests/fixtures/desktopDownloadHarness.tsx"></script></body></html>',
  )
  browsers.chromium = await chromium.launch({ headless: true })
  // The other engines run the clipboard-call test when they are installed
  // locally; a missing browser is reported as a skip, not a failure.
  for (const [name, engine] of Object.entries({ firefox, webkit })) {
    try {
      browsers[name] = await engine.launch({ headless: true })
    } catch {
      browsers[name] = null
    }
  }
})

after(async () => {
  for (const browser of Object.values(browsers)) await browser?.close()
  await server?.close()
})

/**
 * Opens the section as a visitor on `platform`, with the clipboard replaced by
 * a recorder that resolves, or rejects when `clipboard` is "reject".
 *
 * The download resolver answers 204, so clicking a download anchor runs the
 * real navigation without leaving the page (as the real 302 to an attachment
 * does not) and without reaching a backend.
 */
async function openSection(
  t,
  { engine = "chromium", platform = "darwin", clipboard = "resolve" } = {},
) {
  const context = await browsers[engine].newContext({
    userAgent: USER_AGENTS[platform],
  })
  t.after(() => context.close())
  const page = await context.newPage()
  page.setDefaultTimeout(10000)
  const errors = []
  page.on("pageerror", (error) => errors.push(error.message))
  t.after(() => assert.deepEqual(errors, []))

  await page.addInitScript((mode) => {
    // Chromium's architecture hint would turn Linux into "Browse releases"
    // on an arm64 test machine; the tests pin the x86-64 path.
    Object.defineProperty(Navigator.prototype, "userAgentData", {
      get: () => undefined,
    })
    window.__clipboardWrites = []
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText(text) {
          window.__clipboardWrites.push(text)
          return mode === "reject"
            ? Promise.reject(new DOMException("denied", "NotAllowedError"))
            : Promise.resolve()
        },
      },
    })
  }, clipboard)

  await page.route(`${baseUrl}/`, (route) =>
    route.fulfill({ contentType: "text/html", body: harnessHtml }),
  )
  const downloads = []
  await page.route("**/api/v1/desktop/download**", (route) => {
    downloads.push(new URL(route.request().url()).searchParams.toString())
    return route.fulfill({ status: 204 })
  })
  await page.goto(baseUrl)
  return {
    page,
    downloads,
    writes: () => page.evaluate(() => window.__clipboardWrites),
    deepLink: `cinna://connect?${new URLSearchParams({ server: baseUrl })}`,
  }
}

const isPrimary = (locator) =>
  locator.evaluate((el) => el.classList.contains("bg-primary"))

for (const engine of ["chromium", "firefox", "webkit"]) {
  test(`[${engine}] Download writes the connect link to the clipboard`, async (t) => {
    if (!browsers[engine]) {
      t.skip(`${engine} is not installed for this Playwright version`)
      return
    }
    const { page, downloads, writes, deepLink } = await openSection(t, {
      engine,
    })
    await page.getByRole("link", { name: /Download for macOS/ }).click()
    await expect.poll(writes).toHaveLength(1)
    assert.deepEqual(await writes(), [deepLink])
    // The anchor was not prevented: the resolver was asked for the build.
    await expect
      .poll(() => downloads)
      .toEqual(["os=darwin&arch=arm64&kind=dmg"])
    await expect(
      page.getByRole("status").filter({ hasText: NOTE }),
    ).toBeVisible()
  })
}

test("Linux download writes the same link and shows the offer note", async (t) => {
  const { page, writes, deepLink } = await openSection(t, { platform: "linux" })
  await page.getByRole("link", { name: /Download for Linux/ }).click()
  await expect.poll(writes).toEqual([deepLink])
  await expect(
    page.getByRole("status").filter({ hasText: LINUX_NOTE }),
  ).toBeVisible()
  await expect(page.getByRole("status").filter({ hasText: NOTE })).toHaveCount(
    0,
  )
  // No Server URL field was mentioned, so the address line keeps its wording.
  await expect(
    page.getByText("…or enter this server address in the app:"),
  ).toBeVisible()
})

for (const engine of ["chromium", "firefox", "webkit"]) {
  test(`[${engine}] the write happens synchronously inside the click handler`, async (t) => {
    if (!browsers[engine]) {
      t.skip(`${engine} is not installed for this Playwright version`)
      return
    }
    // Safari drops the user activation after the first await, so nothing may
    // be awaited before writeText. A script-dispatched click runs its
    // listeners synchronously and no microtask runs until this evaluate
    // returns, so a write made after any await is not counted yet.
    const { page } = await openSection(t, { engine })
    const writesWhenClickReturns = await page.evaluate(() => {
      const link = [...document.querySelectorAll("a")].find((a) =>
        a.textContent.includes("Download for macOS"),
      )
      link.click()
      return window.__clipboardWrites.length
    })
    assert.equal(writesWhenClickReturns, 1)
  })
}

test("Browse Linux releases does not touch the clipboard", async (t) => {
  const context = await browsers.chromium.newContext({
    userAgent: USER_AGENTS.linux,
  })
  t.after(() => context.close())
  const page = await context.newPage()
  await page.addInitScript(() => {
    Object.defineProperty(Navigator.prototype, "userAgentData", {
      get: () => ({
        getHighEntropyValues: async () => ({ architecture: "arm" }),
      }),
    })
    window.__writes = 0
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: () => {
          window.__writes += 1
          return Promise.resolve()
        },
      },
    })
  })
  await page.route(`${baseUrl}/`, (route) =>
    route.fulfill({ contentType: "text/html", body: harnessHtml }),
  )
  await page.route("https://github.com/**", (route) =>
    route.fulfill({ body: "releases" }),
  )
  await page.goto(baseUrl)
  const browse = page.getByRole("link", { name: "Browse Linux releases" })
  const popup = page.waitForEvent("popup")
  await browse.click()
  await (await popup).close()
  assert.equal(await page.evaluate(() => window.__writes), 0)
  await expect(page.getByText("Already installed?")).toBeVisible()
})

test("a rejected write shows no toast and still moves the emphasis", async (t) => {
  const { page, writes } = await openSection(t, { clipboard: "reject" })
  const download = page.getByRole("link", { name: /Download for macOS/ })
  const open = page.getByRole("link", { name: "Open Cinna Desktop" })
  assert.equal(await isPrimary(download), true)
  assert.equal(await isPrimary(open), false)
  await expect(page.getByText("Already installed?")).toBeVisible()

  await download.click()
  await expect.poll(writes).toHaveLength(1)
  await expect(page.getByText("Installed it?")).toBeVisible()
  await expect.poll(() => isPrimary(download)).toBe(false)
  assert.equal(await isPrimary(open), true)
  // Give a rejected promise's handler the chance to (wrongly) toast or note.
  await page.waitForTimeout(300)
  await expect(page.locator("[data-sonner-toast]")).toHaveCount(0)
  // No link was copied, so the address line keeps its original wording.
  await expect(
    page.getByText("…or enter this server address in the app:"),
  ).toBeVisible()
  await expect(page.getByRole("status").filter({ hasText: NOTE })).toHaveCount(
    0,
  )
})

test("the note appears only after a successful write", async (t) => {
  const { page } = await openSection(t)
  await expect(page.getByRole("status").filter({ hasText: NOTE })).toHaveCount(
    0,
  )
  await expect(
    page.getByText("…or enter this server address in the app:"),
  ).toBeVisible()
  await page.getByRole("link", { name: /Download for macOS/ }).click()
  await expect(page.getByRole("status").filter({ hasText: NOTE })).toBeVisible()
  await expect(page.locator("[data-sonner-toast]")).toHaveCount(0)
  // The address line stops competing with the note for the same field.
  await expect(
    page.getByText("…or type the plain address there instead:"),
  ).toBeVisible()
})

for (const platform of ["darwin", "unknown"]) {
  test(`[${platform}] the note appearing moves nothing on the page`, async (t) => {
    const { page } = await openSection(t, { platform })
    const targets = [
      page.getByRole("link", { name: /Download for macOS/ }),
      page.getByRole("button", { name: "Intel Mac?" }),
      page.getByRole("link", { name: "Open Cinna Desktop" }),
      page.getByRole("button", { name: "Copy the server address" }),
    ]
    if (platform === "unknown")
      targets.push(page.getByRole("link", { name: /Download for Linux/ }))
    const boxes = () => Promise.all(targets.map((l) => l.boundingBox()))
    const before = await boxes()
    await targets[0].click()
    await expect(
      page.getByRole("status").filter({ hasText: NOTE }),
    ).toBeVisible()
    assert.deepEqual(await boxes(), before)
  })
}

test("with both platforms shown, the note sits under the button clicked", async (t) => {
  const { page } = await openSection(t, { platform: "unknown" })
  const statuses = page.getByRole("status")
  await expect(statuses).toHaveCount(2)
  const linux = page.getByRole("link", { name: /Download for Linux/ })
  await linux.click()
  await expect(statuses.nth(1)).toHaveText(LINUX_NOTE)
  await expect(statuses.nth(0)).toHaveText("")
  const linuxBox = await linux.boundingBox()
  const noteBox = await statuses.nth(1).boundingBox()
  assert.ok(noteBox.y >= linuxBox.y + linuxBox.height)
})
