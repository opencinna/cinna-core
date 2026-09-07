#!/usr/bin/env node
/**
 * ui-shot — screenshot a route of the running dev app for UI review.
 *
 * Used by cinna-core-ui-developer and cinna-core-ui-designer, and only when
 * docs/development/frontend/ui_ux_guidelines.md §9 says a surface needs a
 * visual check (new composition, new route/tab, expand/collapse or
 * data-length-dependent layout). Simple pattern instances are reviewed from
 * the JSX; do not screenshot them.
 *
 *   node frontend/scripts/ui-shot.mjs --url "/settings#channels" --out channels
 *   node frontend/scripts/ui-shot.mjs --url "/agent/<id>#configuration" \
 *        --click "text=Add Handover" --out handover-add --width 1440,1024,390
 *   node frontend/scripts/ui-shot.mjs --url "/admin/users" --dark --out users-dark
 *
 * Options
 *   --url <path>        route (with optional #hash), relative to the app origin  [required]
 *   --out <name>        file stem; writes frontend/.ui-shots/<name>-<width>.png  [default: derived from url]
 *   --click <selector>  Playwright selector to click after load; repeatable, applied in order
 *   --wait <ms>         settle time after navigation / each click              [default: 1500]
 *   --width <list>      comma-separated viewport widths                        [default: 1440,1024]
 *   --theme <mode>      light | dark — written to the app's own theme store  [default: app default]
 *   --element <sel>     screenshot only this element (e.g. "[role=dialog]") — use for dialogs/sheets
 *   --no-full           viewport-only screenshot instead of full page
 *
 * Full-page mode expands the app's inner scroll containers (the layout is
 * `h-screen overflow-hidden` with a scrolling page region, so a naive
 * fullPage capture stops at 900px) before capturing. Dialogs and sheets are
 * position:fixed and do not benefit from that — pass --element for them.
 *
 * Login: uses UI_SHOT_EMAIL / UI_SHOT_PASSWORD when set, otherwise
 * FIRST_SUPERUSER / FIRST_SUPERUSER_PASSWORD from the repo-root .env.
 * Credentials are never printed.
 *
 * App origin: the docker frontend on :5173 is nginx serving the *image's*
 * build — it shows the tree as of the last image build, not the working
 * tree, so it is never used by default. The tool reuses a Vite dev server
 * on UI_SHOT_DEV_PORT (default 5199) if one is listening, otherwise starts
 * one for the run and stops it afterwards. UI_SHOT_APP overrides all of
 * that (use it only for a server you know serves the working tree).
 * API origin: UI_SHOT_API (default http://localhost:8000).
 *
 * The script never creates or modifies data. Whatever the dev database holds
 * is what you see; review compositions, not content.
 */
import { chromium } from "playwright"
import { spawn } from "node:child_process"
import { existsSync, mkdirSync, readFileSync } from "node:fs"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const here = dirname(fileURLToPath(import.meta.url))
const frontendDir = resolve(here, "..")
const repoRoot = resolve(frontendDir, "..")
const outDir = join(frontendDir, ".ui-shots")

function parseArgs(argv) {
  const args = { click: [], width: "1440,1024", wait: "1500", full: true, theme: null, element: null }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    const next = () => argv[++i]
    if (a === "--url") args.url = next()
    else if (a === "--out") args.out = next()
    else if (a === "--click") args.click.push(next())
    else if (a === "--wait") args.wait = next()
    else if (a === "--width") args.width = next()
    else if (a === "--theme") args.theme = next()
    else if (a === "--dark") args.theme = "dark"
    else if (a === "--light") args.theme = "light"
    else if (a === "--element") args.element = next()
    else if (a === "--no-full") args.full = false
    else if (a === "--help" || a === "-h") args.help = true
    else throw new Error(`unknown argument: ${a}`)
  }
  return args
}

function readDotEnv() {
  const p = join(repoRoot, ".env")
  if (!existsSync(p)) return {}
  const out = {}
  for (const line of readFileSync(p, "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/)
    if (!m) continue
    let v = m[2]
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1)
    out[m[1]] = v
  }
  return out
}

function credentials() {
  const env = readDotEnv()
  const email = process.env.UI_SHOT_EMAIL || env.FIRST_SUPERUSER
  const password = process.env.UI_SHOT_PASSWORD || env.FIRST_SUPERUSER_PASSWORD
  if (!email || !password) {
    throw new Error("no credentials: set UI_SHOT_EMAIL/UI_SHOT_PASSWORD or FIRST_SUPERUSER* in .env")
  }
  return { email, password }
}

async function login(api, { email, password }) {
  const body = new URLSearchParams({ username: email, password })
  const res = await fetch(`${api}/api/v1/login/access-token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  })
  if (!res.ok) throw new Error(`login failed: HTTP ${res.status} (check credentials / API origin)`)
  const json = await res.json()
  if (!json.access_token) throw new Error("login failed: no access_token in response")
  return json.access_token
}

/**
 * Make the page as tall as its content: the layout pins the shell to the
 * viewport (`h-screen overflow-hidden`) and scrolls an inner region, so
 * Playwright's fullPage capture would otherwise stop at the viewport height.
 * Every element that actually scrolls (scrollHeight > clientHeight) and every
 * fixed-height ancestor up to <body> is switched to auto height / visible
 * overflow. Fixed-position overlays (dialogs) are left alone.
 */
async function expandScrollContainers(page) {
  await page.evaluate(() => {
    const relax = (el) => {
      el.style.setProperty("height", "auto", "important")
      el.style.setProperty("max-height", "none", "important")
      el.style.setProperty("min-height", "0", "important")
      el.style.setProperty("overflow", "visible", "important")
    }
    const all = Array.from(document.querySelectorAll("body *"))
    const scrollers = all.filter((el) => {
      const cs = getComputedStyle(el)
      if (cs.position === "fixed") return false
      const oy = cs.overflowY
      return (oy === "auto" || oy === "scroll" || oy === "hidden") && el.scrollHeight > el.clientHeight + 2
    })
    for (const el of scrollers) {
      let node = el
      while (node && node !== document.body) {
        relax(node)
        node = node.parentElement
      }
    }
    document.documentElement.style.setProperty("height", "auto", "important")
    document.body.style.setProperty("height", "auto", "important")
    document.body.style.setProperty("overflow", "visible", "important")
  })
  await page.waitForTimeout(200)
}

async function isUp(origin) {
  try {
    const res = await fetch(origin, { signal: AbortSignal.timeout(1500) })
    return res.ok
  } catch {
    return false
  }
}

/**
 * Resolve the app origin: UI_SHOT_APP if set; else a Vite dev server on the
 * dev port (reused when already listening, started otherwise). Returns the
 * origin and a stop() that kills a server this run started.
 */
async function resolveApp() {
  if (process.env.UI_SHOT_APP) return { app: process.env.UI_SHOT_APP.replace(/\/$/, ""), stop: () => {} }
  const port = parseInt(process.env.UI_SHOT_DEV_PORT || "5199", 10)
  const origin = `http://localhost:${port}`
  if (await isUp(origin)) return { app: origin, stop: () => {} }
  const child = spawn("npx", ["vite", "--port", String(port), "--strictPort", "--host", "127.0.0.1", "--clearScreen", "false"], {
    cwd: frontendDir,
    stdio: ["ignore", "ignore", "pipe"],
  })
  let stderr = ""
  child.stderr.on("data", (d) => (stderr += d.toString()))
  const deadline = Date.now() + 45000
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      // Another run may have won the port between our probe and our spawn; reuse it.
      if (await isUp(origin)) return { app: origin, stop: () => {} }
      throw new Error(`vite exited early: ${stderr.slice(-300)}`)
    }
    if (await isUp(origin)) break
    await new Promise((r) => setTimeout(r, 500))
  }
  if (!(await isUp(origin))) {
    child.kill()
    throw new Error(`vite did not come up on ${origin} within 45s: ${stderr.slice(-300)}`)
  }
  // First request compiles the module graph; give it a moment so the first capture is not blank.
  await new Promise((r) => setTimeout(r, 1500))
  console.error(`ui-shot: started vite dev server on ${origin} for this run`)
  return { app: origin, stop: () => child.kill() }
}

async function main() {
  const args = parseArgs(process.argv.slice(2))
  if (args.help || !args.url) {
    console.log(readFileSync(fileURLToPath(import.meta.url), "utf8").split("*/")[0].replace(/^\/\*\*?\s?/, ""))
    process.exit(args.help ? 0 : 1)
  }
  const { app, stop } = await resolveApp()
  const api = (process.env.UI_SHOT_API || "http://localhost:8000").replace(/\/$/, "")
  const widths = args.width.split(",").map((w) => parseInt(w.trim(), 10)).filter((w) => w > 0)
  const wait = parseInt(args.wait, 10)
  const stem = args.out || args.url.replace(/^\//, "").replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "") || "root"

  const token = await login(api, credentials())
  mkdirSync(outDir, { recursive: true })

  const browser = await chromium.launch()
  const written = []
  try {
    for (const width of widths) {
      const context = await browser.newContext({
        viewport: { width, height: 900 },
        colorScheme: args.theme === "light" ? "light" : "dark",
        deviceScaleFactor: 1,
      })
      await context.addInitScript(
        ({ t, theme }) => {
          try {
            window.localStorage.setItem("access_token", t)
            // The app persists its own mode under this key (see main.tsx ThemeProvider storageKey).
            if (theme) window.localStorage.setItem("vite-ui-theme", theme)
          } catch {}
        },
        { t: token, theme: args.theme },
      )
      const page = await context.newPage()
      const errors = []
      page.on("pageerror", (e) => errors.push(String(e)))
      await page.goto(`${app}${args.url}`, { waitUntil: "networkidle" })
      await page.waitForTimeout(wait)
      for (const sel of args.click) {
        await page.locator(sel).first().click()
        await page.waitForTimeout(wait)
      }
      const file = join(outDir, `${stem}-${width}.png`)
      if (args.element) {
        await page.locator(args.element).first().screenshot({ path: file })
      } else {
        if (args.full) await expandScrollContainers(page)
        await page.screenshot({ path: file, fullPage: args.full })
      }
      written.push(file)
      if (errors.length) console.error(`[${width}] page errors: ${errors.length} (first: ${errors[0].slice(0, 200)})`)
      await context.close()
    }
  } finally {
    await browser.close()
    stop()
  }
  for (const f of written) console.log(f)
}

main().catch((e) => {
  console.error(`ui-shot: ${e.message}`)
  process.exit(1)
})
