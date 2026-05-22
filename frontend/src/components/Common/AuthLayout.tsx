import { Appearance } from "@/components/Common/Appearance"
import { useTheme } from "@/components/theme-provider"
import authLogoDark from "/assets/images/auth-logo-dark.png"
import authLogoLight from "/assets/images/auth-logo-light.png"
import { Footer } from "./Footer"

interface AuthLayoutProps {
  children: React.ReactNode
}

export function AuthLayout({ children }: AuthLayoutProps) {
  const { resolvedTheme } = useTheme()
  const isDark = resolvedTheme === "dark"
  const logo = isDark ? authLogoDark : authLogoLight

  return (
    <div className="grid min-h-svh lg:grid-cols-2">
      <div className="bg-cyan-50 dark:bg-stone-950 relative hidden lg:flex lg:items-center lg:justify-center">
        <img
          src={logo}
          alt="Logo"
          className="h-32 w-32 drop-shadow-xl"
        />
      </div>
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
  )
}
