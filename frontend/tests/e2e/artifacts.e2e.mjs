// Artifacts end-to-end (plan §4 / §7): runs against the real stack.
//
//   docker compose build frontend && docker compose up -d
//   npm run test:artifacts:e2e
//
// E2E_BASE_URL (default http://localhost:5173) is the SPA; E2E_API_URL
// (default http://localhost:8000) is the API the SPA is built against. The
// superuser credentials come from the repo's .env (FIRST_SUPERUSER*).
//
// Covers core.md §4 security tests 1 and 2 — a published site cannot reach the
// app's token, DOM or API, another artifact, or the network, while its own
// files, ES modules and WebAssembly work — both framed in the viewer and opened
// top-level; then a public link opening signed out, with and without a code.
import assert from "node:assert/strict"
import {
  copyFileSync,
  cpSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
} from "node:fs"
import os from "node:os"
import path from "node:path"
import { after, before, test } from "node:test"
import { fileURLToPath } from "node:url"
import { chromium } from "@playwright/test"

const here = path.dirname(fileURLToPath(import.meta.url))
const fixtures = path.join(here, "..", "fixtures", "artifacts")
const repoRoot = path.join(here, "..", "..", "..")
const BASE = (process.env.E2E_BASE_URL || "http://localhost:5173").replace(
  /\/$/,
  "",
)
const API = (process.env.E2E_API_URL || "http://localhost:8000").replace(
  /\/$/,
  "",
)

function readEnv(name) {
  if (process.env[name]) return process.env[name]
  const text = readFileSync(path.join(repoRoot, ".env"), "utf8")
  const line = text.split("\n").find((l) => l.startsWith(`${name}=`))
  if (!line) throw new Error(`${name} is not set in .env`)
  return line
    .slice(name.length + 1)
    .trim()
    .replace(/^["']|["']$/g, "")
}

let token
let browser
const created = []

async function api(method, url, body, init = {}) {
  const response = await fetch(`${API}/api/v1${url}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body && !(body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
    },
    body:
      body instanceof FormData ? body : body ? JSON.stringify(body) : undefined,
    ...init,
  })
  const text = await response.text()
  if (!response.ok)
    throw new Error(`${method} ${url} → ${response.status} ${text}`)
  return text ? JSON.parse(text) : null
}

function listFiles(dir, prefix = "") {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name)
    const rel = prefix ? `${prefix}/${name}` : name
    return statSync(full).isDirectory() ? listFiles(full, rel) : [rel]
  })
}

/**
 * Publish a folder as `files[]`, each named by its relative path. `folder` is
 * a fixture name or an absolute path; `skip` drops files by relative path.
 */
async function publishFixture(folder, title, { skip = [] } = {}) {
  const dir = path.isAbsolute(folder) ? folder : path.join(fixtures, folder)
  const form = new FormData()
  form.append("title", title)
  form.append("source_kind", "upload")
  for (const rel of listFiles(dir)) {
    if (skip.includes(rel)) continue
    form.append("files", new Blob([readFileSync(path.join(dir, rel))]), rel)
  }
  const artifact = await api("POST", "/artifacts", form)
  created.push(artifact.id)
  return artifact
}

before(async () => {
  const login = await fetch(`${API}/api/v1/login/access-token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      username: readEnv("FIRST_SUPERUSER"),
      password: readEnv("FIRST_SUPERUSER_PASSWORD"),
    }),
  })
  assert.equal(login.status, 200, "superuser login")
  token = (await login.json()).access_token
  assert.ok(token, "an access token (MFA must be off for the superuser)")
  browser = await chromium.launch({ headless: true })
})

/**
 * The sql.js dashboard fixture with the sql.js runtime vendored in from
 * node_modules (the ~1 MB binaries are not committed).
 */
let sqljsDir
function sqljsDashboard() {
  if (sqljsDir) return sqljsDir
  sqljsDir = mkdtempSync(path.join(os.tmpdir(), "artifacts-sqljs-"))
  cpSync(path.join(fixtures, "sqljs-dashboard"), sqljsDir, { recursive: true })
  const dist = path.join(here, "..", "..", "node_modules", "sql.js", "dist")
  for (const file of ["sql-wasm.js", "sql-wasm.wasm"]) {
    copyFileSync(path.join(dist, file), path.join(sqljsDir, file))
  }
  return sqljsDir
}

after(async () => {
  if (sqljsDir) rmSync(sqljsDir, { recursive: true, force: true })
  for (const id of created) {
    await api("DELETE", `/artifacts/${id}`).catch(() => undefined)
  }
  await browser?.close()
})

/** A browser context signed in as the superuser (the SPA reads localStorage). */
async function signedInPage(t) {
  const context = await browser.newContext()
  t.after(() => context.close())
  await context.addInitScript(
    ([origin, value]) => {
      if (window.location.origin === origin) {
        window.localStorage.setItem("access_token", value)
      }
    },
    [BASE, token],
  )
  const page = await context.newPage()
  page.setDefaultTimeout(20000)
  return page
}

// Network checks: "blocked" must mean fetch itself rejected (CSP refusal →
// TypeError), never an HTTP error status from a server it did reach.
const NETWORK_CHECKS = ["apiRelative", "apiAppOrigin", "otherArtifact"]

function assertVerdict(verdict, where) {
  for (const check of NETWORK_CHECKS) {
    assert.equal(
      verdict.blocked[check]?.error,
      "TypeError",
      `${where}: ${check} must be refused by the browser, got ${JSON.stringify(verdict.blocked[check])}`,
    )
  }
  for (const [check, result] of Object.entries(verdict.blocked)) {
    assert.equal(
      result.ok,
      false,
      `${where}: ${check} must be blocked, got ${JSON.stringify(result)}`,
    )
  }
  for (const [check, result] of Object.entries(verdict.allowed)) {
    assert.equal(
      result.ok,
      true,
      `${where}: ${check} must work, got ${JSON.stringify(result)}`,
    )
  }
}

test("a published site is sandboxed, framed in the viewer and opened top-level", async (t) => {
  const probe = await publishFixture("sandbox-probe", "E2E sandbox probe")
  assert.equal(probe.kind, "site")
  const other = await publishFixture("single", "E2E other artifact")
  const otherView = await api("POST", `/artifacts/${other.id}/view`)
  const params = {
    otherCap: `${otherView.content_base}report.md`,
    appOrigin: BASE,
  }
  // Sanity: the other cap URL does work from outside the sandbox.
  assert.equal((await fetch(params.otherCap)).status, 200)

  const page = await signedInPage(t)
  await page.goto(`${BASE}/artifact/${probe.id}`)
  const iframe = page.locator('iframe[title="E2E sandbox probe"]')
  await iframe.waitFor()
  assert.equal(
    await iframe.getAttribute("sandbox"),
    "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals allow-forms",
  )

  const frame = await iframe.elementHandle().then((h) => h.contentFrame())
  await frame.waitForFunction(() => typeof window.runProbe === "function")
  const framed = await frame.evaluate((p) => window.runProbe(p), params)
  assertVerdict(framed, "framed")
  assert.ok(
    framed.blocked.appDocument,
    "the parent-document check runs when framed",
  )
  assert.match(await frame.locator("#result").textContent(), /"blocked"/)

  // Top-level: the raw capability URL in its own tab ("Open in a new tab").
  const fullScreen = await page
    .getByRole("link", { name: "Open in a new tab" })
    .getAttribute("href")
  assert.ok(fullScreen?.includes("/a/v/"), "Open full screen is the cap URL")
  const top = await page.context().newPage()
  await top.goto(`${fullScreen}#${encodeURIComponent(JSON.stringify(params))}`)
  await top.waitForFunction(
    () => document.getElementById("result")?.textContent !== "pending",
  )
  const topVerdict = JSON.parse(await top.locator("#result").textContent())
  assertVerdict(topVerdict, "top-level")
  // window.opener is null at top level, so that check is framed-only.
  assert.equal(topVerdict.blocked.appDocument, undefined)
})

test("a files artifact browses its tree; markdown stays inert", async (t) => {
  const artifact = await publishFixture("single", "E2E files")
  assert.equal(artifact.kind, "files")
  const page = await signedInPage(t)
  await page.goto(`${BASE}/artifact/${artifact.id}`)
  // README.md / index.md are absent, so the first file is selected.
  await page.getByRole("navigation", { name: "Files" }).waitFor()
  await page.getByRole("button", { name: "report.md" }).click()
  await page.getByRole("heading", { name: "Quarterly report" }).waitFor()
  assert.equal(await page.evaluate(() => window.__markdownScriptRan), undefined)
  // The relative image is rewritten to the cap URL.
  const src = await page.locator('article img[alt="Chart"]').getAttribute("src")
  assert.ok(src?.includes("/a/v/") && src.endsWith("/pic.svg"), src)
  // A relative link to a manifest file selects it in-app.
  await page.getByRole("link", { name: "the table" }).click()
  await page.waitForURL(/file=t\.csv/)
  await page.getByText("North").waitFor()
  // PDF smoke: pdf.js renders the first page.
  await page.getByRole("button", { name: "doc.pdf" }).click()
  await page.locator('canvas[aria-label="Page 1"]').waitFor()
})

test("a sql.js dashboard runs its query; without index.html it is a file browser", async (t) => {
  const dir = sqljsDashboard()
  const site = await publishFixture(dir, "E2E sqljs dashboard")
  assert.equal(site.kind, "site")
  const page = await signedInPage(t)
  await page.goto(`${BASE}/artifact/${site.id}`)
  const iframe = page.locator('iframe[title="E2E sqljs dashboard"]')
  await iframe.waitFor()
  const frame = await iframe.elementHandle().then((h) => h.contentFrame())
  await frame.waitForSelector("body[data-ready]")
  assert.equal(
    await frame.locator("body").getAttribute("data-ready"),
    "true",
    await frame.locator("#status").textContent(),
  )
  const rows = await frame.locator("#totals tbody tr").allTextContents()
  assert.deepEqual(rows, ["North2000", "East1430", "South1350", "West610"])

  const files = await publishFixture(dir, "E2E sqljs files", {
    skip: ["index.html"],
  })
  assert.equal(files.kind, "files")
  await page.goto(`${BASE}/artifact/${files.id}`)
  await page.getByRole("navigation", { name: "Files" }).waitFor()
  await page.getByRole("button", { name: "data.sqlite" }).waitFor()
  assert.equal(
    await page.locator("iframe").count(),
    0,
    "no site frame for a files artifact",
  )
})

test("a public link opens signed out, and a coded one asks for its code", async (t) => {
  const artifact = await publishFixture("dashboard", "E2E dashboard")
  const open = await api("POST", `/artifacts/${artifact.id}/links`, {
    label: "E2E",
  })
  const coded = await api("POST", `/artifacts/${artifact.id}/links`, {
    label: "E2E coded",
    with_code: true,
  })

  const context = await browser.newContext()
  t.after(() => context.close())
  const page = await context.newPage()
  page.setDefaultTimeout(20000)
  await page.goto(`${BASE}/s/${open.token}`)
  await page.getByRole("heading", { name: "E2E dashboard" }).waitFor()
  assert.equal(
    await page.evaluate(() => localStorage.getItem("access_token")),
    null,
  )
  const frame = await page
    .locator('iframe[title="E2E dashboard"]')
    .elementHandle()
    .then((h) => h.contentFrame())
  await frame.waitForSelector('body[data-ready="true"]')
  assert.equal(await frame.locator("#sales tbody tr").count(), 4)

  await page.goto(`${BASE}/s/${coded.token}`)
  await page.getByText("Enter the 4-digit code you were given.").waitFor()
  const wrong = coded.code === "0000" ? "1111" : "0000"
  await page.getByLabel("Digit 1").fill(wrong[0])
  await page.getByLabel("Digit 2").fill(wrong[1])
  await page.getByLabel("Digit 3").fill(wrong[2])
  await page.getByLabel("Digit 4").fill(wrong[3])
  await page.getByRole("button", { name: "Continue" }).click()
  await page.getByText("Incorrect code. 2 attempts left.").waitFor()
  assert.ok(
    page.url().includes(`/s/${coded.token}`),
    "a wrong code does not sign anyone out",
  )
  for (const [i, digit] of [...coded.code].entries()) {
    await page.getByLabel(`Digit ${i + 1}`).fill(digit)
  }
  await page.getByRole("button", { name: "Continue" }).click()
  await page.locator('iframe[title="E2E dashboard"]').waitFor()

  await api("DELETE", `/artifacts/${artifact.id}/links/${open.id}`)
  await page.goto(`${BASE}/s/${open.token}`)
  await page.getByText("This link doesn't exist or was turned off.").waitFor()
})
