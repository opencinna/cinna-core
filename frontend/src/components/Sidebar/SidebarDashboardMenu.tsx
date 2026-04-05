import { LayoutDashboard, Check, Settings } from "lucide-react"
import { Link as RouterLink, useRouterState, useNavigate } from "@tanstack/react-router"
import { useQuery } from "@tanstack/react-query"

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"
import { DashboardsService } from "@/client"
import { cn } from "@/lib/utils"

export const SidebarDashboardSwitcher = () => {
  const { isMobile, setOpenMobile } = useSidebar()
  const router = useRouterState()
  const navigate = useNavigate()
  const currentPath = router.location.pathname

  const { data: dashboards } = useQuery({
    queryKey: ["userDashboards"],
    queryFn: () => DashboardsService.listDashboards(),
  })

  const currentDashboardMatch = currentPath.match(/^\/dashboards\/([^/]+)$/)
  const currentDashboardId = currentDashboardMatch?.[1]
  const activeDashboard = dashboards?.find((d) => d.id === currentDashboardId)

  const activeName = activeDashboard?.name || "Dashboards"

  const handleMenuClose = () => {
    if (isMobile) {
      setOpenMobile(false)
    }
  }

  return (
    <>
      <SidebarMenuItem>
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <SidebarMenuButton tooltip="Dashboards">
              <LayoutDashboard className="size-4 text-muted-foreground" />
              <span>{activeName}</span>
              <span className="sr-only">Switch dashboard</span>
            </SidebarMenuButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            side={isMobile ? "top" : "right"}
            align="end"
            className="w-(--radix-dropdown-menu-trigger-width) min-w-56"
          >
            {/* User dashboards */}
            {dashboards && dashboards.length > 0 ? (
              dashboards.map((dashboard) => {
                const isActive = currentDashboardId === dashboard.id
                return (
                  <DropdownMenuItem asChild key={dashboard.id}>
                    <RouterLink
                      to="/dashboards/$dashboardId"
                      params={{ dashboardId: dashboard.id }}
                      onClick={handleMenuClose}
                      className={cn(
                        "flex items-center justify-between cursor-pointer",
                        isActive && "bg-accent"
                      )}
                    >
                      <div className="flex items-center truncate">
                        <LayoutDashboard className="mr-2 h-4 w-4 shrink-0" />
                        <span className="truncate">{dashboard.name}</span>
                      </div>
                      {isActive && <Check className="h-4 w-4 shrink-0" />}
                    </RouterLink>
                  </DropdownMenuItem>
                )
              })
            ) : (
              <DropdownMenuItem disabled className="text-muted-foreground text-sm">
                No dashboards yet
              </DropdownMenuItem>
            )}

            <DropdownMenuSeparator />

<DropdownMenuItem onClick={() => navigate({ to: "/settings", hash: "interface" })}>
              <Settings className="mr-2 h-4 w-4" />
              Manage Dashboards
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </>
  )
}
