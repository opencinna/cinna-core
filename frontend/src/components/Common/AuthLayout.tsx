import { useState } from "react"

import { AuthBrandScope } from "@/components/BrandTheme/AuthBrandScope"
import { BrandLogo } from "@/components/BrandTheme/BrandLogo"
import type { BrandModeLook } from "@/components/BrandTheme/brandTheme"
import { Appearance } from "@/components/Common/Appearance"
import { useTheme } from "@/components/theme-provider"
import { useBrandTheme } from "@/hooks/useBrandTheme"
import { cn } from "@/lib/utils"
import authLogoDark from "/assets/images/auth-logo-dark.png"
import authLogoLight from "/assets/images/auth-logo-light.png"
import { Footer } from "./Footer"

interface AuthLayoutProps {
  children: React.ReactNode
}

export function AuthLayout({ children }: AuthLayoutProps) {
  const { resolvedTheme } = useTheme()
  // Loading and errors render stock (null): the brand only ever repaints.
  const { theme } = useBrandTheme()

  return (
    <AuthBrandScope theme={theme}>
      <div className="grid min-h-svh lg:grid-cols-2">
        <DecorativePanel
          isDark={resolvedTheme === "dark"}
          brandName={theme?.name ?? null}
          brand={theme ? theme.modes[resolvedTheme] : null}
        />
        <div className="flex flex-col gap-4 p-6 md:p-10">
          <div className="flex justify-end">
            <Appearance />
          </div>
          <div className="flex flex-1 items-center justify-center">
            <div className="w-full max-w-xs">{children}</div>
          </div>
          <Footer />
        </div>
      </div>
    </AuthBrandScope>
  )
}

interface DecorativePanelProps {
  isDark: boolean
  brandName: string | null
  /** This mode's look, or null for stock. */
  brand: BrandModeLook | null
}

/**
 * The left half at `lg`. Stock: the product mark on the stock fill. With a
 * brand: the brand's background image (or its raised surface), and its logo
 * in place of the product mark when it ships one. Geometry never changes.
 */
function DecorativePanel({ isDark, brandName, brand }: DecorativePanelProps) {
  // Keyed by URL so switching mode retries the other mode's asset.
  const [failedBackground, setFailedBackground] = useState<string | null>(null)
  const stockMark = (
    <img
      src={isDark ? authLogoDark : authLogoLight}
      alt="Logo"
      className="relative h-32 w-32 drop-shadow-xl"
    />
  )

  const background =
    brand?.backgroundUrl && brand.backgroundUrl !== failedBackground
      ? brand.backgroundUrl
      : null

  let fill = "bg-cyan-50 dark:bg-stone-950"
  if (brand) fill = background ? "bg-background" : "bg-card"

  return (
    <div
      className={cn(
        fill,
        "relative hidden lg:flex lg:items-center lg:justify-center",
      )}
    >
      {background && (
        <img
          src={background}
          alt=""
          aria-hidden="true"
          className="absolute inset-0 h-full w-full object-cover object-center"
          onError={() => setFailedBackground(background)}
        />
      )}
      {brand?.logoUrl && brandName ? (
        <BrandLogo src={brand.logoUrl} alt={brandName} fallback={stockMark} />
      ) : (
        stockMark
      )}
    </div>
  )
}
