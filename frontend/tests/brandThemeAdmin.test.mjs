// Brand themes C3: the Brand theme card on Admin → Server Configuration →
// Interface (`/admin/server-configuration#interface`).
//
// Runs the production card (`BrandThemeCard`) and the preview mock in
// Chromium through a Vite harness. Drafts carry the backend's shared
// `valid/full` served fixture as their `document`; every API call is answered
// here from an in-memory list, at the harness's split-host base.
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
const DRAFTS = "/api/v1/admin/brand-theme/drafts"
const ADMIN = "/admin/server-configuration"

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

/** "#2e88b8" → "rgb(46, 136, 184)", as computed styles report it. */
function rgb(hex) {
  const n = Number.parseInt(hex.slice(1), 16)
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`
}

let server
let baseUrl
let harnessHtml
let browser
let issues
let brandTheme

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
    '<html><body><div id="root"></div><script type="module" src="/tests/fixtures/brandThemeAdminHarness.tsx"></script></body></html>',
  )
  issues = await server.ssrLoadModule(
    "/src/components/Admin/BrandTheme/brandThemeIssues.ts",
  )
  brandTheme = await server.ssrLoadModule(
    "/src/components/BrandTheme/brandTheme.ts",
  )
  browser = await chromium.launch({ headless: true })
})

after(async () => {
  await browser?.close()
  await server?.close()
})

/** A stored theme as the list returns it; the document is the fixture. */
function draft(id, overrides = {}) {
  return {
    id,
    status: "draft",
    name: "Acme",
    revision: null,
    created_at: "2026-11-06T10:00:00",
    created_by_email: "ada@example.com",
    published_at: null,
    published_by_email: null,
    document: SERVED,
    ...overrides,
  }
}

function published(id, overrides = {}) {
  return draft(id, {
    status: "published",
    revision: 3,
    published_at: "2026-11-06T11:00:00",
    published_by_email: "admin@example.com",
    ...overrides,
  })
}

/** The fixture with every accent (and so the primary button) recoloured. */
function recoloured(accent) {
  const doc = structuredClone(SERVED)
  for (const mode of ["light", "dark"]) doc.theme.modes[mode].accent = accent
  return doc
}

/**
 * Opens the admin page over an in-memory drafts list. `handlers` override
 * the upload, publish, delete and unpublish answers; every request is logged
 * in `calls` as `METHOD path`.
 */
async function openAdmin(
  t,
  { drafts = [], maxDrafts = 20, mode = "light", handlers = {} } = {},
) {
  const { page, calls } = await openPage(t, ADMIN, { mode })
  const state = { drafts: [...drafts] }
  await page.route(`${API}/**`, async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    calls.push(`${method} ${url.pathname}`)
    if (method === "GET" && url.pathname === DRAFTS) {
      return route.fulfill({
        json: {
          data: state.drafts,
          count: state.drafts.length,
          max_drafts: maxDrafts,
        },
      })
    }
    if (method === "POST" && url.pathname === DRAFTS) {
      const answer = await handlers.upload?.(request, state)
      return route.fulfill(answer ?? { json: { id: null, errors: [] } })
    }
    const publishMatch = url.pathname.match(`^${DRAFTS}/([^/]+)/publish$`)
    if (method === "POST" && publishMatch) {
      const answer = await handlers.publish?.(publishMatch[1], state)
      if (answer) return route.fulfill(answer)
      const id = publishMatch[1]
      state.drafts = state.drafts.map((d) =>
        d.id === id
          ? {
              ...d,
              status: "published",
              revision: 7,
              published_at: "2026-11-06T12:00:00",
              published_by_email: "admin@example.com",
            }
          : { ...d, status: "draft" },
      )
      return route.fulfill({ json: state.drafts.find((d) => d.id === id) })
    }
    const deleteMatch = url.pathname.match(`^${DRAFTS}/([^/]+)$`)
    if (method === "DELETE" && deleteMatch) {
      state.drafts = state.drafts.filter((d) => d.id !== deleteMatch[1])
      return route.fulfill({ status: 204 })
    }
    if (
      method === "POST" &&
      url.pathname === "/api/v1/admin/brand-theme/unpublish"
    ) {
      state.drafts = state.drafts.map((d) => ({ ...d, status: "draft" }))
      return route.fulfill({ status: 204 })
    }
    return serveAsset(route, url)
  })
  await page.goto(`${baseUrl}${ADMIN}#interface`)
  return { page, calls, state }
}

async function openPage(t, pathname, { mode = "light", init } = {}) {
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
    ({ mode, init }) => {
      localStorage.setItem("vite-ui-theme", mode)
      if (init) window.__previews = init
    },
    { mode, init },
  )
  await page.route(`${baseUrl}${pathname}`, (route) =>
    route.fulfill({ contentType: "text/html", body: harnessHtml }),
  )
  // The previews' assets; the admin page routes the whole API itself.
  await page.route(`${API}/api/v1/brand-theme/assets/**`, (route) =>
    serveAsset(route, new URL(route.request().url())),
  )
  return { page, calls: [] }
}

function serveAsset(route, url) {
  const asset = ASSETS.get(url.pathname.split("/").pop())
  if (url.pathname.startsWith("/api/v1/brand-theme/assets/") && asset) {
    return route.fulfill({ contentType: asset[0], body: asset[1] })
  }
  return route.fulfill({ status: 404 })
}

const row = (page, name = "Acme") =>
  page.getByRole("button", { name: `Preview the brand theme ${name}` })
const menu = (page, name = "Acme") =>
  page.getByRole("button", { name: `Actions for the brand theme ${name}` })
const previewScope = (page, mode, name = "Acme") =>
  page.getByRole("img", { name: `${mode} mode preview of ${name}` })

/** Picks loose files in the upload dialog's "Choose files" input. */
async function pickFiles(page, files) {
  await page.locator('[data-testid="upload-source-files"]').setInputFiles(
    files.map(([name, body]) => ({
      name,
      mimeType: "application/octet-stream",
      buffer: Buffer.from(body),
    })),
  )
}

const THEME_JSON = readFileSync(path.join(fixtureDir, "theme.json"))

test("issue grouping: modes, logo and background by mode, the rest general", () => {
  const { groupIssues, issueGroup } = issues
  assert.equal(issueGroup("modes.dark.text.muted"), "dark")
  assert.equal(issueGroup("logo.dark"), "dark")
  assert.equal(issueGroup("background.dark"), "dark")
  assert.equal(issueGroup("modes.light"), "light")
  assert.equal(issueGroup("background.light"), "light")
  for (const path of [
    "schema",
    "name",
    "copy.newChatHeading",
    "background",
    "package",
    "theme.json",
    "fonts",
    "modes.darker",
  ]) {
    assert.equal(issueGroup(path), "general", path)
  }
  const grouped = groupIssues([
    { path: "logo.light" },
    { path: "modes.dark.accent" },
    { path: "name" },
    { path: "modes.dark.border" },
  ])
  assert.deepEqual(
    grouped.map((g) => [g.label, g.issues.map((i) => i.path)]),
    [
      ["General", ["name"]],
      ["Dark", ["modes.dark.accent", "modes.dark.border"]],
      ["Light", ["logo.light"]],
    ],
  )
  assert.deepEqual(groupIssues([]), [])
})

test("upload refused: grouped errors in the dialog, nothing listed", async (t) => {
  let body = ""
  const { page } = await openAdmin(t, {
    handlers: {
      upload: (request) => {
        body = request.postDataBuffer().toString("latin1")
        return {
          json: {
            id: null,
            errors: [
              {
                path: "modes.dark.text.muted",
                code: "low_contrast",
                message: "Make text.muted lighter.",
              },
              { path: "name", code: "name_too_long", message: "Shorten it." },
              {
                path: "logo.light",
                code: "logo_too_small",
                message: "Use a taller logo.",
              },
            ],
            warnings: [
              {
                path: "logo.dark",
                code: "svg_sanitised",
                message: "assets/logo.svg: removed <script>.",
              },
            ],
          },
        }
      },
    },
  })
  await expect(page.getByText(/^No themes yet\./)).toBeVisible()
  await page.getByRole("button", { name: "Upload" }).click()
  const dialog = page.getByRole("dialog", { name: "Upload theme package" })
  await pickFiles(page, [
    ["theme.json", THEME_JSON],
    ["logo-light.png", "png"],
  ])
  await dialog.getByRole("button", { name: "Upload" }).click()

  const alert = dialog.getByRole("alert").first()
  await expect(alert).toContainText(
    "Nothing was saved — fix 3 problems and upload again.",
  )
  // The refusal's notes stay visible while the admin fixes the package.
  const notes = dialog.getByRole("alert").nth(1)
  await expect(notes).toContainText("Also 1 note (not blocking).")
  await expect(notes).toContainText("removed <script>")
  const headings = await alert.locator("h4").allTextContents()
  assert.deepEqual(headings, ["General · 1", "Dark · 1", "Light · 1"])
  await expect(alert).toContainText("modes.dark.text.muted")
  await expect(alert).toContainText("Make text.muted lighter.")
  // Loose files: theme.json as `theme`, the rest as `files`.
  assert.match(body, /name="theme"; filename="theme.json"/)
  assert.match(body, /name="files"; filename="logo-light.png"/)
  assert.doesNotMatch(body, /name="package"/)

  // The dialog and the pick stay; nothing was listed.
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText(/2 files/)).toBeVisible()
  await expect(page.getByText(/^No themes yet\./)).toBeVisible()
})

test("upload: loose files without theme.json keep Upload disabled", async (t) => {
  const { page } = await openAdmin(t)
  await page.getByRole("button", { name: "Upload" }).click()
  const dialog = page.getByRole("dialog", { name: "Upload theme package" })
  await pickFiles(page, [["logo.svg", "<svg/>"]])
  await expect(
    dialog.getByText("theme.json is missing from the picked files."),
  ).toBeVisible()
  await expect(dialog.getByRole("button", { name: "Upload" })).toBeDisabled()
})

test("upload accepted: the draft is listed and its preview shows the notes", async (t) => {
  let body = ""
  const { page } = await openAdmin(t, {
    handlers: {
      upload: (request, state) => {
        body = request.postDataBuffer().toString("latin1")
        state.drafts = [draft("d1"), ...state.drafts]
        return {
          json: {
            id: "d1",
            errors: [],
            warnings: [
              {
                path: "logo.dark",
                code: "svg_sanitised",
                message: "A <script> element was removed.",
              },
            ],
          },
        }
      },
    },
  })
  await page.getByRole("button", { name: "Upload" }).click()
  await pickFiles(page, [["acme.zip", "PK"]])
  await page
    .getByRole("dialog", { name: "Upload theme package" })
    .getByRole("button", { name: "Upload" })
    .click()

  await expect(page.getByText("Draft Acme uploaded with 1 note")).toBeVisible()
  // A zip goes as the `package`.
  assert.match(body, /name="package"; filename="acme.zip"/)
  const preview = page.getByRole("dialog", { name: "Acme" })
  await expect(preview).toBeVisible()
  await expect(preview.getByRole("alert")).toContainText("Uploaded with 1 note")
  await expect(preview.getByRole("alert")).toContainText("Dark · 1")
  await expect(previewScope(preview, "Dark")).toBeVisible()
  await expect(previewScope(preview, "Light")).toBeVisible()

  await preview.getByRole("button", { name: "Close" }).first().click()
  await expect(row(page)).toBeVisible()
  await expect(page.locator("body")).not.toHaveCSS("pointer-events", "none")
  await expect(
    page.getByText(
      "The look of the sign-in pages and Cinna Desktop · 1 of 20 kept",
    ),
  ).toBeVisible()
})

test("publish happens from the preview; the menu's Publish only opens it", async (t) => {
  const { page, calls } = await openAdmin(t, { drafts: [draft("d1")] })
  await expect(
    page.getByText("Nothing is published, so this server shows stock Cinna."),
  ).toBeVisible()

  await menu(page).click()
  await page.getByRole("menuitem", { name: "Publish" }).click()
  const preview = page.getByRole("dialog", { name: "Acme" })
  await expect(preview).toBeVisible()
  assert.ok(!calls.some((c) => c.endsWith("/publish")), "nothing published yet")

  await preview.getByRole("button", { name: "Publish" }).click()
  await expect(page.getByText("Acme published as revision 7")).toBeVisible()
  await expect(preview).toHaveCount(0)
  assert.ok(calls.includes(`POST ${DRAFTS}/d1/publish`))

  // The row now carries the live-making fact, and the notice is gone.
  await expect(row(page)).toContainText("rev 7")
  await expect(row(page)).toContainText(/Published .+ago/)
  await expect(
    page.getByText("Nothing is published, so this server shows stock Cinna."),
  ).toHaveCount(0)
  // Nothing is left open or inert under the closed dialog.
  await expect(page.getByRole("menu")).toHaveCount(0)
  await expect(page.locator("body")).not.toHaveCSS("pointer-events", "none")
})

test("publish refused (409): the preview shows the grouped errors", async (t) => {
  const { page } = await openAdmin(t, {
    drafts: [draft("d1")],
    handlers: {
      publish: () => ({
        status: 409,
        json: {
          errors: [
            {
              path: "modes.light.accent",
              code: "low_contrast",
              message: "Darken the accent.",
            },
          ],
        },
      }),
    },
  })
  await row(page).click()
  const preview = page.getByRole("dialog", { name: "Acme" })
  await preview.getByRole("button", { name: "Publish" }).click()
  const alert = preview.getByRole("alert")
  await expect(alert).toContainText(
    "This draft no longer passes validation — the rules have tightened since it was uploaded.",
  )
  assert.deepEqual(await alert.locator("h4").allTextContents(), ["Light · 1"])
  await expect(alert).toContainText("Darken the accent.")
  await expect(preview.getByRole("button", { name: "Publish" })).toBeDisabled()
  await expect(preview).toBeVisible()
  await expect(
    page.getByText("Nothing is published, so this server shows stock Cinna."),
  ).toBeVisible()
})

test("unpublish asks first, then the server shows stock Cinna", async (t) => {
  const { page, calls } = await openAdmin(t, {
    drafts: [published("p1"), draft("d2", { name: "Beta" })],
  })
  const unpublishFromMenu = async () => {
    await menu(page, "Acme").click()
    await page.getByRole("menuitem", { name: "Unpublish", exact: true }).click()
  }
  await unpublishFromMenu()
  const confirm = page.getByRole("alertdialog", { name: "Unpublish Acme?" })
  await expect(confirm).toContainText("Everyone returns to stock Cinna")
  await confirm.getByRole("button", { name: "Cancel" }).click()
  await expect(confirm).toHaveCount(0)
  assert.ok(!calls.some((c) => c.endsWith("/unpublish")))
  // The menu closed as the confirm opened: nothing reappears after Cancel.
  await expect(page.getByRole("menu")).toHaveCount(0)
  await expect(page.locator("body")).not.toHaveCSS("pointer-events", "none")

  await unpublishFromMenu()
  await page.getByRole("button", { name: "Unpublish theme" }).click()
  await expect(
    page.getByText("Theme unpublished — this server shows stock Cinna"),
  ).toBeVisible()
  await expect(
    page.getByText("Nothing is published, so this server shows stock Cinna."),
  ).toBeVisible()
  assert.ok(calls.includes("POST /api/v1/admin/brand-theme/unpublish"))
})

test("unpublish from the published row's menu: the row owns the confirm", async (t) => {
  const { page, calls } = await openAdmin(t, { drafts: [published("p1")] })
  await menu(page).click()
  await page.getByRole("menuitem", { name: "Unpublish", exact: true }).click()
  const confirm = page.getByRole("alertdialog", { name: "Unpublish Acme?" })
  await confirm.getByRole("button", { name: "Unpublish theme" }).click()
  await expect(confirm).toHaveCount(0)
  assert.ok(calls.includes("POST /api/v1/admin/brand-theme/unpublish"))
  await expect(page.getByRole("menu")).toHaveCount(0)
  await expect(page.locator("body")).not.toHaveCSS("pointer-events", "none")
  // Now a draft: its menu offers Publish and a live Delete.
  await menu(page).click()
  await expect(page.getByRole("menuitem", { name: "Publish" })).toBeVisible()
  await expect(
    page.getByRole("menuitem", { name: "Delete draft" }),
  ).not.toHaveAttribute("aria-disabled", "true")
})

test("one Brand theme card; the published row carries the publish facts and opens its preview", async (t) => {
  const { page } = await openAdmin(t, {
    drafts: [draft("d2", { name: "Beta" }), published("p1")],
  })
  await expect(page.getByText("Brand theme", { exact: true })).toHaveCount(1)
  await expect(page.getByText("Published theme")).toHaveCount(0)
  await expect(page.getByText("Drafts", { exact: true })).toHaveCount(0)

  // Pinned first although the list returns it second.
  const rows = page.getByRole("button", { name: /^Preview the brand theme / })
  await expect(rows.first()).toHaveAccessibleName(
    "Preview the brand theme Acme",
  )
  const live = row(page, "Acme")
  await expect(live).toContainText("rev 3")
  await expect(live).toContainText(/Published .+ago/)
  await expect(live).toContainText("Published by admin@example.com")
  await expect(live).toContainText("Uploaded by ada@example.com")
  await expect(row(page, "Beta")).toContainText(/Uploaded .+ago/)
  await expect(
    page.getByText("Nothing is published, so this server shows stock Cinna."),
  ).toHaveCount(0)

  await live.click()
  const preview = page.getByRole("dialog", { name: "Acme" })
  await expect(preview).toBeVisible()
  await expect(previewScope(preview, "Dark")).toBeVisible()
})

test("an old published theme behind more than five newer drafts still renders first", async (t) => {
  const newer = Array.from({ length: 6 }, (_, i) =>
    draft(`d${i}`, {
      name: `Draft ${i}`,
      created_at: `2026-11-06T1${i}:00:00`,
    }),
  )
  const { page } = await openAdmin(t, {
    drafts: [
      ...newer,
      published("p1", { name: "Live", created_at: "2026-10-01T10:00:00" }),
    ],
  })
  const rows = page.getByRole("button", { name: /^Preview the brand theme / })
  await expect(rows).toHaveCount(5)
  await expect(rows.first()).toHaveAccessibleName(
    "Preview the brand theme Live",
  )
  await expect(rows.nth(1)).toHaveAccessibleName(
    "Preview the brand theme Draft 0",
  )
  await page.getByRole("button", { name: "Show all (7)" }).click()
  const sheet = page.getByRole("dialog", { name: "Brand themes" })
  const sheetRows = sheet.getByRole("button", {
    name: /^Preview the brand theme /,
  })
  await expect(sheetRows).toHaveCount(7)
  await expect(sheetRows.first()).toHaveAccessibleName(
    "Preview the brand theme Live",
  )
})

test("delete: disabled with its reason on the published row, confirmed on a draft", async (t) => {
  const { page, calls } = await openAdmin(t, {
    drafts: [published("p1"), draft("d2", { name: "Beta" })],
  })
  await menu(page, "Acme").click()
  const blocked = page.getByRole("menuitem", { name: "Unpublish to delete" })
  await expect(blocked).toBeVisible()
  await expect(blocked).toHaveAttribute("aria-disabled", "true")
  await page.keyboard.press("Escape")

  await menu(page, "Beta").click()
  await page.getByRole("menuitem", { name: "Delete draft" }).click()
  const confirm = page.getByRole("alertdialog", { name: "Delete draft Beta?" })
  await confirm.getByRole("button", { name: "Delete draft" }).click()
  await expect(row(page, "Beta")).toHaveCount(0)
  assert.ok(calls.includes(`DELETE ${DRAFTS}/d2`))
  assert.ok(!calls.includes(`DELETE ${DRAFTS}/p1`))
})

test("the draft cap: an alert and a disabled Upload", async (t) => {
  const { page } = await openAdmin(t, {
    drafts: [draft("d1"), draft("d2")],
    maxDrafts: 2,
  })
  await expect(
    page.getByText("The draft limit is reached (2 of 2)."),
  ).toBeVisible()
  await expect(page.getByRole("button", { name: "Upload" })).toBeDisabled()
})

test("two previews on one page do not leak tokens into each other", async (t) => {
  const { page } = await openPage(t, "/previews", {
    init: [
      { document: recoloured("#2e88b8"), mode: "dark", name: "Blue" },
      { document: recoloured("#b8462e"), mode: "dark", name: "Red" },
    ],
  })
  await page.goto(`${baseUrl}/previews`)
  const blue = previewScope(page, "Dark", "Blue")
  const red = previewScope(page, "Dark", "Red")
  await expect(blue).toBeVisible()

  const ids = await page
    .locator("[data-brand-preview]")
    .evaluateAll((els) =>
      els.map((el) => el.getAttribute("data-brand-preview")),
    )
  assert.equal(new Set(ids).size, 2, "each scope has its own id")
  for (const [scope, hex] of [
    [blue, "#2e88b8"],
    [red, "#b8462e"],
  ]) {
    const css = await scope.locator("style").textContent()
    assert.ok(css.includes(`--primary:${hex};`))
    const button = scope.getByText("Sign in", { exact: true }).last()
    await expect(button).toHaveCSS("background-color", rgb(hex))
  }
  // No global brand selector: the auth pages' scope is untouched.
  assert.equal(await page.locator('[data-skin="brand"]').count(), 0)
})

for (const pageMode of ["light", "dark"]) {
  test(`previews follow their own mode, not the page's (${pageMode} page)`, async (t) => {
    const { page } = await openPage(t, "/previews", {
      mode: pageMode,
      init: [
        { document: SERVED, mode: "light", name: "Acme" },
        { document: SERVED, mode: "dark", name: "Acme" },
      ],
    })
    await page.goto(`${baseUrl}/previews`)
    assert.equal(
      await page
        .locator("html")
        .evaluate((el) => el.classList.contains("dark")),
      pageMode === "dark",
    )
    for (const mode of ["light", "dark"]) {
      const scope = previewScope(page, mode === "light" ? "Light" : "Dark")
      const roles = SERVED.theme.modes[mode]
      await expect(scope).toHaveCSS(
        "background-color",
        rgb(roles["surface.base"]),
      )
      await expect(scope.getByText("Hello")).toHaveCSS(
        "background-color",
        rgb(roles["bubble.user"]),
      )
      await expect(
        scope.getByText("Sign in", { exact: true }).last(),
      ).toHaveCSS("background-color", rgb(roles.accent))
      await expect(scope.locator("img").first()).toHaveAttribute(
        "src",
        `${API}${SERVED.theme.background[mode].url}`,
      )
    }
  })
}

test("a document that fails the parser previews as a notice, not a palette", async (t) => {
  const broken = structuredClone(SERVED)
  broken.theme.modes.dark["bubble.user"] = "red"
  const { page } = await openPage(t, "/previews", {
    init: [{ document: broken, mode: "light", name: "Broken" }],
  })
  await page.goto(`${baseUrl}/previews`)
  await expect(previewScope(page, "Light", "Broken")).toHaveText(
    "This draft can't be previewed",
  )
  assert.equal(await page.locator("[data-brand-preview]").count(), 0)
})

test("scoped builder: only a plain scope id; a bad preview role drops the preview, not C2", () => {
  const { buildScopedBrandCss, parseBrandTheme, parseBrandThemePreview } =
    brandTheme
  // Colours only: asset URLs need the browser's API base, not tested here.
  const colours = structuredClone(SERVED)
  delete colours.theme.logo
  delete colours.theme.background
  const look = parseBrandThemePreview(colours)
  assert.ok(look)
  const css = buildScopedBrandCss(look, "dark", "p1")
  assert.ok(css.startsWith('[data-brand-preview="p1"]{color-scheme:dark;'))
  for (const id of ['p"]{}body{x', "a b", ""]) {
    assert.throws(() => buildScopedBrandCss(look, "dark", id))
  }
  const doc = structuredClone(colours)
  doc.theme.modes.light["chart.8"] = "#ffffff;}body{display:none"
  assert.equal(parseBrandThemePreview(doc), null)
  assert.ok(parseBrandTheme(doc), "the auth pages ignore preview-only roles")
})

test("upload form: a package folder sends only assets/*, loose files send everything", async () => {
  const { uploadFormData } = await server.ssrLoadModule(
    "/src/components/Admin/BrandTheme/UploadBrandThemeDialog.tsx",
  )
  const file = (name) => new File(["x"], name)
  const folder = uploadFormData({
    kind: "files",
    files: ["theme.json", "assets/bg-dark.webp", "README.md", "preview.html"].map(file),
    size: 4,
    suggestedTitle: "acme",
  })
  assert.equal(folder.theme.name, "theme.json")
  assert.deepEqual(folder.files.map((f) => f.name), ["assets/bg-dark.webp"])
  const loose = uploadFormData({
    kind: "files",
    files: ["theme.json", "logo-dark.svg", "bg.webp"].map(file),
    size: 3,
    suggestedTitle: "",
  })
  assert.deepEqual(loose.files.map((f) => f.name), ["logo-dark.svg", "bg.webp"])
  assert.equal(
    uploadFormData({ kind: "files", files: [file("logo.svg")], size: 1, suggestedTitle: "" }),
    null,
  )
})
