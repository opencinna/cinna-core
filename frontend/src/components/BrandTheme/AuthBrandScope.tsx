import { cn } from "@/lib/utils"
import "@/styles/brand-scope.css"

import { BRAND_SCOPE_SKIN, type BrandTheme, buildBrandCss } from "./brandTheme"

interface AuthBrandScopeProps {
  /** The parsed theme, or null for stock Cinna. */
  theme: BrandTheme | null
  className?: string
  children: React.ReactNode
}

/**
 * The element the auth pages render in. With a theme it carries
 * `data-skin="brand"` (on itself, never on <html>), declares the brand's
 * tokens for both modes and paints them opaque, so the default skin's
 * gradient mesh and any skin's body canvas stay behind it.
 *
 * The wrapper is rendered with and without a theme so the subtree never
 * remounts when the theme query settles (a half-typed form survives).
 */
export function AuthBrandScope({
  theme,
  className,
  children,
}: AuthBrandScopeProps) {
  return (
    <div
      data-skin={theme ? BRAND_SCOPE_SKIN : undefined}
      className={cn(theme && "bg-background text-foreground", className)}
    >
      {theme && <style>{buildBrandCss(theme)}</style>}
      {children}
    </div>
  )
}
