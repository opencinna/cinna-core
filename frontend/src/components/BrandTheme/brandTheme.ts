import type { BrandThemeGetBrandThemeResponse } from "@/client"
import { resolveApiBase } from "@/utils"

/**
 * The server's published brand theme, parsed for the auth pages (C2).
 *
 * Role names come from the backend's single source,
 * `backend/app/services/brand_theme/schema.py` (`ROLES`). This file lists only
 * the roles the web reads; the rest of the scheme (surface.input, bubble.user,
 * chart.6–8, logo.from/to, …) is deliberately ignored.
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

export interface BrandModeLook {
  tokens: Record<BrandToken, string>
  /** Absolute URL, or null when absent or not a brand asset path. */
  logoUrl: string | null
  backgroundUrl: string | null
}

export interface BrandTheme {
  name: string
  modes: Record<BrandMode, BrandModeLook>
}

function assetUrl(ref: { url?: unknown } | null | undefined): string | null {
  const path = ref?.url
  if (typeof path !== "string" || !ASSET_PATH.test(path)) return null
  return `${resolveApiBase()}${path}`
}

function parseTokens(
  roles: Record<string, unknown> | undefined,
): Record<BrandToken, string> | null {
  const tokens = {} as Record<BrandToken, string>
  for (const [token, role] of Object.entries(BRAND_TOKEN_ROLES)) {
    const value = roles?.[role]
    if (typeof value !== "string" || !HEX.test(value)) return null
    tokens[token as BrandToken] = value
  }
  return tokens
}

/**
 * The payload of `GET /api/v1/brand-theme`, or null for stock Cinna.
 *
 * Any color that fails the hex check drops the **whole** theme (a half-applied
 * palette is worse than none). An asset path that fails its check drops that
 * asset only; the colors stay.
 */
export function parseBrandTheme(
  payload: BrandThemeGetBrandThemeResponse | null | undefined,
): BrandTheme | null {
  const theme = payload ? payload.theme : undefined
  if (!theme) return null
  const modes = {} as Record<BrandMode, BrandModeLook>
  for (const mode of MODES) {
    const tokens = parseTokens(theme.modes?.[mode])
    if (!tokens) return null
    modes[mode] = {
      tokens,
      logoUrl: assetUrl(theme.logo?.[mode]),
      backgroundUrl: assetUrl(theme.background?.[mode]),
    }
  }
  // `||`: an empty name must still give the logo an accessible name.
  return { name: theme.name || "Logo", modes }
}

/** The scope selector. Only `AuthBrandScope` puts this attribute on the page. */
export const BRAND_SCOPE_SKIN = "brand"

function declarations(tokens: Record<BrandToken, string>): string {
  return Object.entries(tokens)
    .map(([token, value]) => `${token}:${value};`)
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
