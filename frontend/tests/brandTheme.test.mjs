// Brand themes C2: the auth layout and the desktop consent shell, with and
// without a published brand theme.
//
// Runs the production `AuthLayout` and `NativeAuthConsentPage` in Chromium
// through a Vite harness. The theme is the backend's shared `valid/full`
// fixture (the exact payload `GET /api/v1/brand-theme` serves); every API
// call is answered here, at the harness's split-host base `http://api.test`.
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import path from "node:path"
import { after, before, test } from "node:test"
import { fileURLToPath } from "node:url"
import { chromium, expect } from "@playwright/test"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react-swc"
import { createServer } from "vite"

const frontendRoot = fileURLToPath(new URL("..", import.meta.url))
const fixtureDir = path.join(
  frontendRoot,
  "../backend/app/services/brand_theme/fixtures/valid/full",
)
const SERVED = JSON.parse(
  readFileSync(path.join(fixtureDir, "served/served.json"), "utf8"),
)
const API = "http://api.test"
const HEX = /^#[0-9a-f]{6}$/

// sha256 → [content type, bytes]: the sanitised SVG the server stores, and
// the package's rasters (stored unchanged).
const ASSETS = new Map(
  Object.entries({
    "logo.dark": "served/assets/" + SERVED.theme.logo.dark.sha256,
    "logo.light": "assets/logo-light.png",
    "background.dark": "assets/bg-dark.webp",
    "background.light": "assets/bg-light.webp",
  }).map(([key, file]) => [
    SERVED.assets[key].sha256,
    [SERVED.assets[key].type, readFileSync(path.join(fixtureDir, file))],
  ]),
)
const assetSrc = (ref) => `${API}${ref.url}`

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
    '<html><body><div id="root"></div><script type="module" src="/tests/fixtures/brandThemeHarness.tsx"></script></body></html>',
  )
  browser = await chromium.launch({ headless: true })
})

after(async () => {
  await browser?.close()
  await server?.close()
})

/** A deep copy of the served fixture, edited by `edit`. */
function servedWith(edit) {
  const payload = structuredClone(SERVED)
  edit(payload)
  return payload
}

/**
 * Opens `pathname` at 1440 × 900 with the brand endpoint answering `theme`:
 * a payload (200), a status (204 = no theme, 500 = failure).
 */
async function open(
  t,
  pathname,
  { theme = SERVED, mode = "light", skin = null, hold = false } = {},
) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  })
  t.after(() => context.close())
  const page = await context.newPage()
  page.setDefaultTimeout(10000)
  const errors = []
  page.on("pageerror", (error) => errors.push(error.message))
  t.after(() => assert.deepEqual(errors, []))

  await page.addInitScript(
    ({ mode, skin }) => {
      localStorage.setItem("vite-ui-theme", mode)
      if (skin) localStorage.setItem("vite-ui-skin", skin)
    },
    { mode, skin },
  )
  await page.route(`${baseUrl}/${pathname.slice(1)}`, (route) =>
    route.fulfill({ contentType: "text/html", body: harnessHtml }),
  )
  const calls = []
  // `hold`: the theme answers only once the test calls `release()`.
  let release = () => {}
  const held = hold
    ? new Promise((resolve) => {
        release = resolve
      })
    : Promise.resolve()
  await page.route(`${API}/**`, async (route) => {
    const url = new URL(route.request().url())
    calls.push(url.pathname)
    if (url.pathname === "/api/v1/brand-theme") {
      await held
      if (typeof theme === "number") return route.fulfill({ status: theme })
      return route.fulfill({ status: 200, json: theme })
    }
    const sha = url.pathname.split("/").pop()
    const asset = ASSETS.get(sha)
    if (url.pathname.startsWith("/api/v1/brand-theme/assets/") && asset) {
      return route.fulfill({ contentType: asset[0], body: asset[1] })
    }
    return route.fulfill({ status: 404 })
  })
  await page.goto(`${baseUrl}${pathname}`)
  return { page, calls, release }
}

const scope = (page) => page.locator('[data-skin="brand"]')
const brandCss = (page) =>
  page.locator('[data-skin="brand"] > style').textContent()

/** Every `--token:value` declaration in the generated block. */
function declarations(css) {
  return [...css.matchAll(/(--[a-z0-9-]+):([^;}]*)/g)].map((m) => [m[1], m[2]])
}

async function settled(page, calls) {
  await expect.poll(() => calls.includes("/api/v1/brand-theme")).toBe(true)
  await expect(page.getByRole("button", { name: "Log In" })).toBeVisible()
}

test("no theme (204): the layout renders stock", async (t) => {
  const { page, calls } = await open(t, "/layout", { theme: 204 })
  await settled(page, calls)

  await expect(scope(page)).toHaveCount(0)
  await expect(page.locator("#root style")).toHaveCount(0)
  const mark = page.getByRole("img", { name: "Logo" })
  await expect(mark).toBeVisible()
  await expect(mark).toHaveClass(/h-32 w-32 drop-shadow-xl/)
  await expect(mark.locator("..")).toHaveClass(/bg-cyan-50 dark:bg-stone-950/)
  assert.equal(await page.locator("html").getAttribute("data-skin"), null)
})

test("theme failure (500): the layout renders stock", async (t) => {
  const { page, calls } = await open(t, "/layout", { theme: 500 })
  await settled(page, calls)

  await expect(scope(page)).toHaveCount(0)
  await expect(page.getByRole("img", { name: "Logo" })).toBeVisible()
})

test("theme: data-skin on the wrapper, hex-only tokens, logo and background from the API base", async (t) => {
  const { page } = await open(t, "/layout")

  await expect(scope(page)).toHaveCount(1)
  assert.equal(
    await scope(page).evaluate((el) => el.tagName),
    "DIV",
    "the scope is the layout's wrapper element",
  )
  assert.equal(
    await page.locator("html").getAttribute("data-skin"),
    null,
    "<html> is never branded",
  )

  const css = await brandCss(page)
  const decls = declarations(css)
  assert.ok(decls.length >= 2 * 25, "both modes declare the mapped tokens")
  for (const [token, value] of decls) {
    assert.match(value, HEX, `${token} is a hex value`)
  }
  assert.ok(!/url\(|var\(|@import|expression/i.test(css))
  assert.ok(
    css.includes(`--background:${SERVED.theme.modes.light["surface.base"]}`),
  )
  assert.ok(
    css.includes(
      `--muted-foreground:${SERVED.theme.modes.dark["text.secondary"]}`,
    ),
  )

  // The scope paints the brand's surface; text inherits the brand's ink.
  await expect(scope(page)).toHaveCSS("background-color", "rgb(255, 255, 255)")
  await expect(scope(page)).toHaveCSS("color", "rgb(26, 26, 26)")

  // The brand logo replaces the stock mark, in the 240 × 62 header box.
  const logo = page.getByRole("img", { name: "Acme" })
  await expect(logo).toBeVisible()
  assert.equal(
    await logo.getAttribute("src"),
    assetSrc(SERVED.theme.logo.light),
  )
  const box = await logo.boundingBox()
  assert.deepEqual([box.width, box.height], [240, 62])
  await expect(page.getByRole("img", { name: "Logo" })).toHaveCount(0)

  // The background fills the panel, decorative, from the API base.
  const background = page.locator('img[alt=""][aria-hidden="true"]')
  await expect(background).toHaveAttribute(
    "src",
    assetSrc(SERVED.theme.background.light),
  )
  await expect(background).toHaveClass(/object-cover/)
})

test("theme, dark mode: the dark tokens and the dark assets apply", async (t) => {
  const { page } = await open(t, "/layout", { mode: "dark" })

  await expect(scope(page)).toHaveCSS("background-color", "rgb(15, 15, 15)")
  await expect(page.getByRole("img", { name: "Acme" })).toHaveAttribute(
    "src",
    assetSrc(SERVED.theme.logo.dark),
  )
  await expect(page.locator('img[aria-hidden="true"]')).toHaveAttribute(
    "src",
    assetSrc(SERVED.theme.background.dark),
  )
})

for (const [label, value] of [
  ["a named color", "red"],
  ["upper-case hex", "#FFFFFF"],
  ["a CSS injection", "#ffffff;}body{display:none"],
]) {
  test(`a malformed color (${label}) drops the whole theme`, async (t) => {
    const theme = servedWith((p) => {
      p.theme.modes.dark["text.primary"] = value
    })
    const { page, calls } = await open(t, "/layout", { theme })
    await settled(page, calls)

    await expect(scope(page)).toHaveCount(0)
    await expect(page.getByRole("img", { name: "Logo" })).toBeVisible()
    await expect(page.getByRole("img", { name: "Acme" })).toHaveCount(0)
  })
}

test("a malformed asset URL drops that asset only; the colors stay", async (t) => {
  const theme = servedWith((p) => {
    p.theme.logo.light.url = "https://evil.test/logo.png"
  })
  const { page } = await open(t, "/layout", { theme })

  await expect(scope(page)).toHaveCount(1)
  await expect(page.getByRole("img", { name: "Logo" })).toBeVisible()
  await expect(page.getByRole("img", { name: "Acme" })).toHaveCount(0)
  await expect(page.locator('img[aria-hidden="true"]')).toHaveAttribute(
    "src",
    assetSrc(SERVED.theme.background.light),
  )
})

test("a logo that fails to load falls back to the stock mark", async (t) => {
  const theme = servedWith((p) => {
    p.theme.logo.light.url = `/api/v1/brand-theme/assets/${"0".repeat(64)}`
  })
  const { page } = await open(t, "/layout", { theme })

  await expect(scope(page)).toHaveCount(1)
  await expect(page.getByRole("img", { name: "Logo" })).toBeVisible()
  await expect(page.getByRole("img", { name: "Acme" })).toHaveCount(0)
})

test("hacker-1980 skin: stock type inside the brand scope, skin effects outside it", async (t) => {
  // Control: without a theme the skin's effects apply on the auth page.
  const stock = await open(t, "/layout", { theme: 204, skin: "hacker-1980" })
  await settled(stock.page, stock.calls)
  await expect(stock.page.getByRole("button", { name: "Log In" })).toHaveCSS(
    "text-transform",
    "uppercase",
  )

  const { page } = await open(t, "/layout", { skin: "hacker-1980" })
  await expect(scope(page)).toHaveCount(1)
  const button = page.getByRole("button", { name: "Log In" })
  await expect(button).toHaveCSS("text-transform", "none")
  await expect(button).toHaveCSS("background-color", "rgb(46, 136, 184)")

  // The stock focus ring survives the Tier C reset.
  const input = page.getByRole("textbox", { name: "Email" })
  await input.focus()
  // `focus-visible:ring-[3px]`, not the skin's 1px `--skin-line` ring.
  await expect(input).toHaveCSS("box-shadow", /0px 0px 0px 3px/)
})

test("consent, branded, with a theme: brand logo above the card, page background", async (t) => {
  const { page } = await open(t, "/consent")

  await expect(page.getByText("Authorize Cinna Desktop")).toBeVisible()
  await expect(scope(page)).toHaveCount(1)
  const logo = page.getByRole("img", { name: "Acme" })
  await expect(logo).toBeVisible()
  const logoBox = await logo.boundingBox()
  const cardBox = await page.locator('[data-slot="card"]').boundingBox()
  assert.ok(logoBox.y + logoBox.height <= cardBox.y, "the logo sits above")
  await expect(page.locator('img[aria-hidden="true"]')).toHaveAttribute(
    "src",
    assetSrc(SERVED.theme.background.light),
  )
})

test("consent, branded, under hacker-1980: the card is a stock card", async (t) => {
  const { page } = await open(t, "/consent", { skin: "hacker-1980" })
  await expect(page.getByText("Authorize Cinna Desktop")).toBeVisible()
  const card = page.locator('[data-slot="card"]')
  await expect(card).toHaveCSS("animation-name", "none")
  await expect(card).toHaveCSS("backdrop-filter", "none")
  await expect(page.locator('[data-slot="card-title"]')).toHaveCSS(
    "text-transform",
    "none",
  )
})

test("consent, branded, no theme: no logo, stock page", async (t) => {
  const { page } = await open(t, "/consent", { theme: 204 })

  await expect(page.getByText("Authorize Cinna Desktop")).toBeVisible()
  await expect(scope(page)).toHaveCount(0)
  await expect(page.getByRole("img")).toHaveCount(0)
})

test("consent, not branded: stock even with a theme, and no theme request", async (t) => {
  const { page, calls } = await open(t, "/consent-unbranded")

  await expect(page.getByText("Authorize Cinna Desktop")).toBeVisible()
  await expect(scope(page)).toHaveCount(0)
  await expect(page.getByRole("img")).toHaveCount(0)
  assert.ok(!calls.some((p) => p.startsWith("/api/v1/brand-theme")))
})

test("the theme arriving repaints the layout without remounting the form", async (t) => {
  const { page, calls, release } = await open(t, "/layout", { hold: true })
  await expect.poll(() => calls.includes("/api/v1/brand-theme")).toBe(true)

  // Loading renders stock, and the form is usable.
  await expect(page.getByRole("img", { name: "Logo" })).toBeVisible()
  await expect(scope(page)).toHaveCount(0)
  const email = page.getByRole("textbox", { name: "Email" })
  await email.fill("ada@example.com")
  await email.evaluate((el) => {
    el.dataset.marker = "original"
  })

  release()
  await expect(scope(page)).toHaveCount(1)
  await expect(page.getByRole("img", { name: "Acme" })).toBeVisible()
  await expect(email).toHaveValue("ada@example.com")
  await expect(email).toHaveAttribute("data-marker", "original")
})

test("consent, branded: the card waits for the theme, so the logo never pops in", async (t) => {
  const { page, calls, release } = await open(t, "/consent", { hold: true })
  await expect.poll(() => calls.includes("/api/v1/brand-theme")).toBe(true)

  // The request info has resolved; the shell still holds the spinner.
  await page.waitForTimeout(300)
  await expect(page.locator('[data-slot="card"]')).toHaveCount(0)
  await expect(page.locator("svg.animate-spin")).toBeVisible()

  release()
  await expect(page.getByText("Authorize Cinna Desktop")).toBeVisible()
  await expect(page.getByRole("img", { name: "Acme" })).toBeVisible()
})

test("Cinna Mobile consent, branded: brand logo above the card", async (t) => {
  const { page } = await open(t, "/consent-mobile")

  await expect(page.getByText("Authorize Cinna Mobile")).toBeVisible()
  await expect(scope(page)).toHaveCount(1)
  await expect(page.getByRole("img", { name: "Acme" })).toBeVisible()
})

test("device-login consent (cinna login): branded shell around the code form", async (t) => {
  const { page } = await open(t, "/device")

  await expect(page.locator('[data-slot="card"]')).toBeVisible()
  await expect(scope(page)).toHaveCount(1)
  const logo = page.getByRole("img", { name: "Acme" })
  await expect(logo).toBeVisible()
  const logoBox = await logo.boundingBox()
  const cardBox = await page.locator('[data-slot="card"]').boundingBox()
  assert.ok(logoBox.y + logoBox.height <= cardBox.y, "the logo sits above")
})

test("device-login consent, no theme: stock page, no logo", async (t) => {
  const { page } = await open(t, "/device", { theme: 204 })

  await expect(page.locator('[data-slot="card"]')).toBeVisible()
  await expect(scope(page)).toHaveCount(0)
  await expect(page.getByRole("img")).toHaveCount(0)
})

test("every role the web reads is a role of the contract (backend schema.ROLES)", async () => {
  // The served fixture is generated from schema.ROLES, so its keys are the
  // contract's role names; a misspelt role here would silently make every
  // theme render stock (or "can't be previewed").
  const { BRAND_TOKEN_ROLES, PREVIEW_ROLES } = await server.ssrLoadModule(
    "/src/components/BrandTheme/brandTheme.ts",
  )
  for (const mode of ["dark", "light"]) {
    const contract = new Set(Object.keys(SERVED.theme.modes[mode]))
    assert.equal(contract.size, 35)
    for (const role of [...Object.values(BRAND_TOKEN_ROLES), ...PREVIEW_ROLES]) {
      assert.ok(contract.has(role), `${role} is not a contract role`)
    }
  }
})

test("a timed-out theme request is not cached as 'no theme': the next focus brands the page", async (t) => {
  const { page, calls, release } = await open(t, "/layout", { hold: true })

  // Past the 3 s timeout: stock, and the failure is not a cached answer.
  await expect(page.getByRole("button", { name: "Log In" })).toBeVisible()
  await page.waitForTimeout(3500)
  await expect(scope(page)).toHaveCount(0)

  release()
  // React Query listens on window for visibilitychange.
  await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")))
  await expect(scope(page)).toHaveCount(1)
  assert.ok(calls.filter((c) => c === "/api/v1/brand-theme").length >= 2)
})
