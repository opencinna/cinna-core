import { Link as RouterLink, useRouterState } from "@tanstack/react-router"
import { Bot, Key, MessageSquare, Bell, ClipboardList, Home } from "lucide-react"
import { useQuery, useQueryClient } from "@tanstack/react-query"

import { Logo } from "@/components/Common/Logo"
import { SidebarWorkspaceSwitcher } from "@/components/Common/WorkspaceSwitcher"
import { AgenticTeamsSwitcher } from "@/components/AgenticTeams/AgenticTeamsSwitcher"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarGroup,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarSeparator,
  useSidebar,
} from "@/components/ui/sidebar"
import useAuth from "@/hooks/useAuth"
import { type Item, Main } from "./Main"
import { User } from "./User"
import { AdminMenu } from "./AdminMenu"
import { SidebarDashboardSwitcher } from "./SidebarDashboardMenu"
import { ActivitiesService } from "@/client"
import { cn } from "@/lib/utils"
import { useMultiEventSubscription, useConnectionStatus, EventTypes } from "@/hooks/useEventBus"

const menuItems: Item[] = [
  { icon: Home, title: "Dashboard", path: "/" },
  { icon: ClipboardList, title: "Tasks", path: "/tasks" },
  { icon: Bot, title: "Agents", path: "/agents" },
  { icon: MessageSquare, title: "Sessions", path: "/sessions" },
  { icon: Key, title: "Credentials", path: "/credentials" },
]

function ActivitiesMenu() {
  const { isMobile, setOpenMobile } = useSidebar()
  const router = useRouterState()
  const currentPath = router.location.pathname
  const queryClient = useQueryClient()
  const connectionStatus = useConnectionStatus()

  const { data: activityStats } = useQuery({
    queryKey: ["activity-stats"],
    queryFn: () => ActivitiesService.getActivityStats(),
    refetchInterval: 10000,
  })

  useMultiEventSubscription(
    [EventTypes.ACTIVITY_CREATED, EventTypes.ACTIVITY_UPDATED, EventTypes.ACTIVITY_DELETED],
    (event) => {
      console.log("[Sidebar] Received activity event:", event.type, event)
      queryClient.invalidateQueries({ queryKey: ["activity-stats"] })
    }
  )

  const handleMenuClick = () => {
    if (isMobile) {
      setOpenMobile(false)
    }
  }

  const isActive = currentPath === "/activities"
  const hasActionRequired = (activityStats?.action_required_count || 0) > 0
  const hasUnread = (activityStats?.unread_count || 0) > 0

  const statusDotColor = {
    connected: "bg-green-500",
    connecting: "bg-yellow-500",
    disconnected: "bg-red-500",
  }[connectionStatus]

  const statusLabel = {
    connected: "Online",
    connecting: "Connecting...",
    disconnected: "Offline",
  }[connectionStatus]

  return (
    <SidebarMenuItem>
      <SidebarMenuButton tooltip={`Activities (${statusLabel})`} isActive={isActive} asChild>
        <RouterLink to="/activities" onClick={handleMenuClick}>
          <span className="relative">
            <Bell
              className={cn(
                "size-4",
                hasUnread && !hasActionRequired && "text-primary",
                hasActionRequired && "text-destructive"
              )}
            />
            <span
              className={cn(
                "absolute -top-0.5 -right-0.5 size-1.5 rounded-full border border-sidebar-background",
                statusDotColor
              )}
            />
          </span>
          <span>Activities</span>
        </RouterLink>
      </SidebarMenuButton>
    </SidebarMenuItem>
  )
}


export function AppSidebar() {
  const { user: currentUser } = useAuth()

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="px-4 py-6 group-data-[collapsible=icon]:px-0 group-data-[collapsible=icon]:items-center">
        <Logo variant="responsive" />
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarWorkspaceSwitcher />
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        <SidebarSeparator />
        <Main items={menuItems} />
      </SidebarContent>
      <SidebarFooter>
        <ActivitiesMenu />
        <SidebarDashboardSwitcher />
        <AgenticTeamsSwitcher />
        {currentUser?.is_superuser && <AdminMenu />}
        <User user={currentUser} />
      </SidebarFooter>
    </Sidebar>
  )
}

export default AppSidebar
