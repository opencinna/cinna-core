import { Loader2 } from "lucide-react"
import { useState } from "react"
import { AuthBrandScope } from "@/components/BrandTheme/AuthBrandScope"
import { BrandLogo } from "@/components/BrandTheme/BrandLogo"
import { useTheme } from "@/components/theme-provider"
import { useBrandTheme } from "@/hooks/useBrandTheme"
import { cn } from "@/lib/utils"

export interface ConsentShellProps {
  /** Paint the server's brand theme (logo, background, colors). */
  branded: boolean
  /** Show the spinner instead of `children` (the request is loading). */
  loading?: boolean
  /** Stock look of the main branch: an opaque page behind the card. */
  paintBackground?: boolean
  children?: React.ReactNode
}

/**
 * The one full-page shell every branch of the browser consent screens
 * (native-app OAuth and `cinna login` device authorization) renders in: the brand's background
 * fixed behind the page, its logo above the card. It holds the spinner until
 * the brand theme settles too, so the logo never pops in above a rendered
 * card. Same element type in every branch, so switching branch never
 * remounts it.
 */
export function ConsentShell({
  branded,
  loading = false,
  paintBackground = false,
  children,
}: ConsentShellProps) {
  const { resolvedTheme } = useTheme()
  const { theme, isPending } = useBrandTheme({ enabled: branded })
  // Keyed by URL so switching mode retries the other mode's asset.
  const [failedBackground, setFailedBackground] = useState<string | null>(null)
  const look = theme?.modes[resolvedTheme]
  const background =
    look?.backgroundUrl && look.backgroundUrl !== failedBackground
      ? look.backgroundUrl
      : null

  return (
    <AuthBrandScope
      theme={theme}
      className={cn(
        "relative isolate flex min-h-svh flex-col items-center justify-center gap-6 p-4",
        paintBackground && "bg-background",
      )}
    >
      {background && (
        <img
          src={background}
          alt=""
          aria-hidden="true"
          className="fixed inset-0 -z-10 h-full w-full object-cover object-center"
          onError={() => setFailedBackground(background)}
        />
      )}
      {loading || isPending ? (
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      ) : (
        <>
          {theme && look?.logoUrl && (
            <BrandLogo src={look.logoUrl} alt={theme.name} fallback={null} />
          )}
          {children}
        </>
      )}
    </AuthBrandScope>
  )
}
