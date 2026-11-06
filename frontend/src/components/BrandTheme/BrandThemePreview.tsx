import { useId, useState } from "react"

import authLogoDark from "/assets/images/auth-logo-dark.png"
import authLogoLight from "/assets/images/auth-logo-light.png"
import { BrandLogo } from "./BrandLogo"
import {
  BRAND_PREVIEW_ATTR,
  type BrandMode,
  type BrandThemePreviewLook,
  buildScopedBrandCss,
  type PreviewRole,
  roleVar,
} from "./brandTheme"

const MODE_WORD: Record<BrandMode, string> = { dark: "Dark", light: "Light" }

const STATUS_KINDS = [
  ["success", "Success"],
  ["warning", "Warning"],
  ["danger", "Error"],
  ["info", "Info"],
] as const

const CHART_SLOTS = [1, 2, 3, 4, 5, 6, 7, 8] as const

/** A preview role as a CSS value: always a property the scope declared. */
const role = (name: PreviewRole) => `var(${roleVar(name)})`

interface BrandThemePreviewProps {
  /** The parsed draft, or null when its document fails the parser. */
  theme: BrandThemePreviewLook | null
  mode: BrandMode
  /** The theme's name as the admin knows it (for the accessible label). */
  name: string
}

/**
 * A mock of what one mode of a brand theme looks like: a sign-in card over
 * the mode's background, and a strip of app chrome (sidebar, a user bubble,
 * status chips, the eight chart slots).
 *
 * The scope carries its own unique `data-brand-preview` id and declares one
 * mode's tokens on itself (`buildScopedBrandCss`), so two previews on a page
 * never share tokens and neither follows the page's own `.dark`. Nothing in
 * here uses a `dark:` variant for the same reason. The mock is `inert`: its
 * fake input and button are pictures, never focus targets.
 */
export function BrandThemePreview({
  theme,
  mode,
  name,
}: BrandThemePreviewProps) {
  // `useId` returns `«r1»` / `:r1:`; the selector wants a plain token.
  const scopeId = `p${useId().replace(/[^A-Za-z0-9_-]/g, "")}`
  const [failedBackground, setFailedBackground] = useState<string | null>(null)
  const label = `${MODE_WORD[mode]} mode preview of ${name}`

  if (!theme) {
    return (
      <div
        role="img"
        aria-label={label}
        className="flex h-[360px] items-center justify-center rounded-lg border border-dashed bg-muted/30 px-4 text-center text-sm text-muted-foreground"
      >
        This draft can't be previewed
      </div>
    )
  }

  const look = theme.modes[mode]
  const background =
    look.backgroundUrl && look.backgroundUrl !== failedBackground
      ? look.backgroundUrl
      : null
  const stockMark = (
    <img
      src={mode === "dark" ? authLogoDark : authLogoLight}
      alt=""
      className="h-[41px] w-[41px]"
    />
  )

  const scope = { [BRAND_PREVIEW_ATTR]: scopeId }
  return (
    <div
      {...scope}
      role="img"
      aria-label={label}
      className="relative h-[360px] overflow-hidden rounded-lg border bg-background text-foreground"
    >
      <style>{buildScopedBrandCss(theme, mode, scopeId)}</style>
      <div aria-hidden="true" inert className="flex h-full flex-col">
        {/* Sign-in zone, over the mode's background or flat surface.base. */}
        <div className="relative flex min-h-0 flex-1 flex-col items-center justify-center gap-3">
          {background && (
            <img
              src={background}
              alt=""
              className="absolute inset-0 h-full w-full object-cover object-center"
              onError={() => setFailedBackground(background)}
            />
          )}
          {look.logoUrl ? (
            <BrandLogo
              src={look.logoUrl}
              alt=""
              fallback={stockMark}
              className="h-[41px] w-[160px]"
            />
          ) : (
            stockMark
          )}
          <div className="relative w-[240px] space-y-2.5 rounded-lg border bg-card p-4 shadow-sm">
            <p className="text-sm font-semibold text-foreground">Sign in</p>
            <div
              className="flex h-7 items-center rounded-md border px-2 text-xs"
              style={{
                backgroundColor: role("surface.input"),
                color: role("text.muted"),
              }}
            >
              you@example.com
            </div>
            <div className="flex h-7 items-center justify-center rounded-md bg-primary text-xs font-medium text-primary-foreground">
              Sign in
            </div>
            <p className="text-[11px]" style={{ color: role("text.muted") }}>
              Forgot your password?
            </p>
          </div>
        </div>

        {/* App chrome strip, on surface.base. */}
        <div className="relative flex h-[100px] shrink-0 gap-3 border-t bg-background p-3">
          <div className="w-16 shrink-0 space-y-1.5 rounded-md bg-card p-2">
            <div className="h-2.5 rounded bg-accent" />
            <div className="h-1.5 w-4/5 rounded-full bg-muted-foreground" />
            <div className="h-1.5 w-3/5 rounded-full bg-muted-foreground" />
          </div>
          <div className="flex min-w-0 flex-1 flex-col justify-between">
            <div
              className="ml-auto w-fit rounded-lg px-2.5 py-1 text-xs text-foreground"
              style={{ backgroundColor: role("bubble.user") }}
            >
              Hello
            </div>
            <div className="flex flex-wrap gap-x-2.5 gap-y-0.5 text-[10px]">
              {STATUS_KINDS.map(([kind, word]) => (
                <span key={kind} className="flex items-center gap-1">
                  <span
                    className="h-1.5 w-1.5 rounded-full"
                    style={{ backgroundColor: role(`status.${kind}`) }}
                  />
                  <span style={{ color: role(`status.${kind}.text`) }}>
                    {word}
                  </span>
                </span>
              ))}
            </div>
            <div className="flex h-2 gap-0.5">
              {CHART_SLOTS.map((slot) => (
                <span
                  key={slot}
                  className="flex-1 rounded-sm"
                  style={{ backgroundColor: role(`chart.${slot}`) }}
                />
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
