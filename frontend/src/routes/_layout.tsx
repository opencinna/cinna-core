import { createFileRoute, Outlet, redirect } from "@tanstack/react-router"
import { createContext, useContext, useState, ReactNode } from "react"
import { Loader2 } from "lucide-react"

import { LoginService } from "@/client"
import AppSidebar from "@/components/Sidebar/AppSidebar"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { isLoggedIn } from "@/hooks/useAuth"
import { useEventBusConnection } from "@/hooks/useEventBus"
import { useBundleEvents } from "@/hooks/useBundleEvents"
import { useRoleEvents } from "@/hooks/useRoleEvents"
import { useNavigationTracker } from "@/hooks/useNavigationHistory"
import AgentUserWelcomeBanner from "@/components/Common/AgentUserWelcomeBanner"

interface HeaderContextType {
  setHeaderContent: (content: ReactNode) => void
}

const HeaderContext = createContext<HeaderContextType | null>(null)

export const usePageHeader = () => {
  const context = useContext(HeaderContext)
  if (!context) {
    throw new Error("usePageHeader must be used within HeaderProvider")
  }
  return context
}

function AuthCheckingPending() {
  return (
    <div className="flex min-h-svh items-center justify-center bg-background">
      <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
    </div>
  )
}

export const Route = createFileRoute("/_layout")({
  component: Layout,
  pendingComponent: AuthCheckingPending,
  beforeLoad: async () => {
    if (!isLoggedIn()) {
      throw redirect({ to: "/login" })
    }
    try {
      await LoginService.testToken()
    } catch (error: any) {
      if (
        error?.status === 401 ||
        error?.status === 403 ||
        error?.status === 404
      ) {
        localStorage.removeItem("access_token")
        throw redirect({ to: "/login" })
      }
      throw error
    }
  },
})

function Layout() {
  const [headerContent, setHeaderContent] = useState<ReactNode>(null)

  // Initialize WebSocket connection for real-time events
  useEventBusConnection()

  // Wire bundle / install events into React Query invalidations.
  useBundleEvents()

  // Phase 3 — react to USER_ROLE_CHANGED events (refetch + re-route).
  useRoleEvents()

  // Track navigation history for Back button support
  useNavigationTracker()

  return (
    <HeaderContext.Provider value={{ setHeaderContent }}>
      <SidebarProvider>
        <AppSidebar />
        <SidebarInset className="flex flex-col h-screen overflow-hidden">
          <header className="sticky top-0 z-10 flex h-16 shrink-0 items-center gap-4 border-b px-4 bg-background/60">
            <SidebarTrigger className="-ml-1 text-muted-foreground" />
            {headerContent && (
              <div className="flex-1 flex items-center justify-between gap-4 min-w-0">
                {headerContent}
              </div>
            )}
          </header>
          <main className="flex-1 flex flex-col min-h-0 min-w-0">
            <AgentUserWelcomeBanner />
            <Outlet />
          </main>
        </SidebarInset>
      </SidebarProvider>
    </HeaderContext.Provider>
  )
}

export default Layout
