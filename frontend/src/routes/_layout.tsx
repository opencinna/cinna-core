import { createFileRoute, Outlet, redirect } from "@tanstack/react-router"
import { createContext, useContext, useState, ReactNode } from "react"

import AppSidebar from "@/components/Sidebar/AppSidebar"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { isLoggedIn } from "@/hooks/useAuth"
import { useEventBusConnection } from "@/hooks/useEventBus"

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

export const Route = createFileRoute("/_layout")({
  component: Layout,
  beforeLoad: async () => {
    if (!isLoggedIn()) {
      throw redirect({
        to: "/login",
      })
    }
  },
})

function Layout() {
  const [headerContent, setHeaderContent] = useState<ReactNode>(null)

  // Initialize WebSocket connection for real-time events
  useEventBusConnection()

  return (
    <HeaderContext.Provider value={{ setHeaderContent }}>
      <SidebarProvider>
        <AppSidebar />
        <SidebarInset className="flex flex-col h-screen">
          <header className="sticky top-0 z-10 flex h-16 shrink-0 items-center gap-4 border-b px-4 bg-background">
            <SidebarTrigger className="-ml-1 text-muted-foreground" />
            {headerContent && (
              <div className="flex-1 flex items-center justify-between gap-4 min-w-0">
                {headerContent}
              </div>
            )}
          </header>
          <main className="flex-1 flex flex-col min-h-0">
            <Outlet />
          </main>
        </SidebarInset>
      </SidebarProvider>
    </HeaderContext.Provider>
  )
}

export default Layout
