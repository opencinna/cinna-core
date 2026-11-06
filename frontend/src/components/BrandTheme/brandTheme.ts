import type { BrandThemeGetBrandThemeResponse } from "@/client"
import { resolveApiBase } from "@/utils"

/**
 * The server's brand theme, parsed for the auth pages (C2) and for the admin
 * page's draft previews (C3).
 *
 * Role names come from the backend's single source,
 * `backend/app/services/brand_theme/schema.py` (`ROLES`). This file lists only
 * the roles the web reads: the auth pages read `BRAND_TOKEN_ROLES`, the
 * preview additionally `PREVIEW_ROLES`; the rest of the scheme (accent.hover,
 * highlight, chart.pos/neg/grid, logo.from/to, …) is deliberately ignored.
 */

export type BrandMode = "light" | "dark"

const MODES: readonly BrandMode[] = ["light", "dark"]

/**
 * shadcn token → brand role. The one and only role map on the web.
 * Two readability corrections (see docs/application/brand_themes/
 * brand_themes_tech.md, "Web sign-in pages"): `--muted-foreground` ←
 * text.secondary, because these pages set body copy in it and text.muted is
 * only validated at 3:1; `--destructive` ← status.danger.text, because it
 * only ever paints error text here.
 *
 * Not mapped, on purpose: `--success-foreground` / `--warning-foreground`
 * (the scheme has no "on status" role, and the auth pages draw status colors
 * as icons only) and the sidebar tokens (no sidebar on these pages).
 */
export const BRAND_TOKEN_ROLES = {
  "--background": "surface.base",
  "--foreground": "text.primary",
  "--card": "surface.raised",
  "--card-foreground": "text.primary",
  "--popover": "surface.raised",
  "--popover-foreground": "text.primary",
  "--muted": "surface.tertiary",
  "--muted-foreground": "text.secondary",
  "--secondary": "surface.tertiary",
  "--secondary-foreground": "text.primary",
  "--accent": "surface.hover",
  "--accent-foreground": "text.primary",
  "--primary": "accent",
  "--primary-foreground": "accent.on",
  "--border": "border",
  "--input": "border",
  "--ring": "accent",
  "--destructive": "status.danger.text",
  "--success": "status.success",
  "--warning": "status.warning",
  "--chart-1": "chart.1",
  "--chart-2": "chart.2",
  "--chart-3": "chart.3",
  "--chart-4": "chart.4",
  "--chart-5": "chart.5",
} as const

export type BrandToken = keyof typeof BRAND_TOKEN_ROLES

/** Normalised by the server; re-checked here so nothing else reaches CSS. */
const HEX = /^#[0-9a-f]{6}$/

/** Content-addressed asset path, exactly as the public endpoint serves it. */
const ASSET_PATH = /^\/api\/v1\/brand-theme\/assets\/[0-9a-f]{64}$/

/**
 * Roles the admin preview mock paints that `BRAND_TOKEN_ROLES` does not map
 * (C3). A list of role names, not a second token map: each is exposed in a
 * preview scope under a property derived from its name (`roleVar`), never
 * under a shadcn token, so the auth pages' map stays the only map.
 */
export const PREVIEW_ROLES = [
  "surface.input",
  "text.muted",
  "bubble.user",
  "status.success",
  "status.warning",
  "status.danger",
  "status.info",
  "status.success.text",
  "status.warning.text",
  "status.danger.text",
  "status.info.text",
  "chart.1",
  "chart.2",
  "chart.3",
  "chart.4",
  "chart.5",
  "chart.6",
  "chart.7",
  "chart.8",
] as const

export type PreviewRole = (typeof PREVIEW_ROLES)[number]

/**
 * One mode's look. `R` names the extra roles the caller asked the parser for;
 * the auth pages ask for none.
 */
export interface BrandModeLook<R extends string = never> {
  tokens: Record<BrandToken, string>
  /** Extra roles, hex-checked like the tokens. */
  roles: Record<R, string>
  /** Absolute URL, or null when absent or not a brand asset path. */
  logoUrl: string | null
  backgroundUrl: string | null
}

export interface BrandTheme<R extends string = never> {
  name: string
  modes: Record<BrandMode, BrandModeLook<R>>
}

/** A draft's look as the admin preview reads it. */
export type BrandThemePreviewLook = BrandTheme<PreviewRole>

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function assetUrl(ref: unknown): string | null {
  const path = isRecord(ref) ? ref.url : undefined
  if (typeof path !== "string" || !ASSET_PATH.test(path)) return null
  return `${resolveApiBase()}${path}`
}

/** `role` → its hex value for every role named, or null if any fails `HEX`. */
function readRoles<R extends string>(
  roles: unknown,
  names: readonly R[],
): Record<R, string> | null {
  const out = {} as Record<R, string>
  for (const name of names) {
    const value = isRecord(roles) ? roles[name] : undefined
    if (typeof value !== "string" || !HEX.test(value)) return null
    out[name] = value
  }
  return out
}

function parseTokens(roles: unknown): Record<BrandToken, string> | null {
  const byRole = readRoles(roles, Object.values(BRAND_TOKEN_ROLES))
  if (!byRole) return null
  const tokens = {} as Record<BrandToken, string>
  for (const [token, role] of Object.entries(BRAND_TOKEN_ROLES)) {
    tokens[token as BrandToken] = byRole[role]
  }
  return tokens
}

/**
 * A served document (`{schema, revision, theme, assets}`) → its look, or null.
 *
 * Any color that fails the hex check drops the **whole** theme (a half-applied
 * palette is worse than none). An asset path that fails its check drops that
 * asset only; the colors stay.
 */
function parseDocument<R extends string>(
  document: unknown,
  extraRoles: readonly R[],
): BrandTheme<R> | null {
  const theme = isRecord(document) ? document.theme : undefined
  if (!isRecord(theme)) return null
  const modesIn = isRecord(theme.modes) ? theme.modes : {}
  const logo = isRecord(theme.logo) ? theme.logo : {}
  const background = isRecord(theme.background) ? theme.background : {}
  const modes = {} as Record<BrandMode, BrandModeLook<R>>
  for (const mode of MODES) {
    const tokens = parseTokens(modesIn[mode])
    const roles = readRoles(modesIn[mode], extraRoles)
    if (!tokens || !roles) return null
    modes[mode] = {
      tokens,
      roles,
      logoUrl: assetUrl(logo[mode]),
      backgroundUrl: assetUrl(background[mode]),
    }
  }
  // `||`: an empty name must still give the logo an accessible name.
  const name = typeof theme.name === "string" ? theme.name : ""
  return { name: name || "Logo", modes }
}

/** The payload of `GET /api/v1/brand-theme`, or null for stock Cinna. */
export function parseBrandTheme(
  payload: BrandThemeGetBrandThemeResponse | null | undefined,
): BrandTheme | null {
  return parseDocument(payload, [])
}

/**
 * A draft's `document` (the same served shape) for the admin preview: the
 * mapped tokens plus `PREVIEW_ROLES`, through the same checks. Null when any
 * of them fails — the preview then says so rather than painting part of it.
 */
export function parseBrandThemePreview(
  document: unknown,
): BrandThemePreviewLook | null {
  return parseDocument(document, PREVIEW_ROLES)
}

/** The scope selector. Only `AuthBrandScope` puts this attribute on the page. */
export const BRAND_SCOPE_SKIN = "brand"

function declarations(entries: Record<string, string>): string {
  return Object.entries(entries)
    .map(([property, value]) => `${property}:${value};`)
    .join("")
}

/**
 * The scope's token block: light under `[data-skin="brand"]`, dark under
 * `.dark [data-skin="brand"]`. Built from parsed tokens only — every key is
 * from `BRAND_TOKEN_ROLES` and every value passed `HEX`.
 */
export function buildBrandCss(theme: BrandTheme): string {
  const scope = `[data-skin="${BRAND_SCOPE_SKIN}"]`
  return (
    `${scope}{${declarations(theme.modes.light.tokens)}}` +
    `.dark ${scope}{${declarations(theme.modes.dark.tokens)}}`
  )
}

/**
 * The attribute a preview scope carries (C3). Unlike the auth pages' scope it
 * is unique per element and its mode is explicit, so several previews on one
 * page never collide and never follow the page's own `.dark` class.
 */
export const BRAND_PREVIEW_ATTR = "data-brand-preview"

const SCOPE_ID = /^[A-Za-z0-9_-]+$/

/** The custom property a `PREVIEW_ROLES` role is exposed under. */
export function roleVar(role: PreviewRole): string {
  return `--role-${role.replace(/\./g, "-")}`
}

/**
 * One preview's token block: the mapped tokens and the preview roles of
 * `mode`, declared on `[data-brand-preview="<scopeId>"]` only. Declared on the
 * scope element itself, they win over anything inherited from the page.
 */
export function buildScopedBrandCss(
  theme: BrandThemePreviewLook,
  mode: BrandMode,
  scopeId: string,
): string {
  if (!SCOPE_ID.test(scopeId)) throw new Error("Invalid preview scope id")
  const look = theme.modes[mode]
  const roles: Record<string, string> = {}
  for (const role of PREVIEW_ROLES) roles[roleVar(role)] = look.roles[role]
  return `[${BRAND_PREVIEW_ATTR}="${scopeId}"]{color-scheme:${mode};${declarations(
    look.tokens,
  )}${declarations(roles)}}`
}
