import { Link as RouterLink, useRouterState } from "@tanstack/react-router"
import { Bot, Home, Key, MessageSquare, Bell, BookOpen } from "lucide-react"
import { useQuery, useQueryClient } from "@tanstack/react-query"

import { SidebarAppearance } from "@/components/Common/Appearance"
import { Logo } from "@/components/Common/Logo"
import { WebSocketStatus } from "@/components/Common/WebSocketStatus"
import { SidebarWorkspaceSwitcher } from "@/components/Common/WorkspaceSwitcher"
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
  useSidebar,
} from "@/components/ui/sidebar"
import useAuth from "@/hooks/useAuth"
import { type Item, Main } from "./Main"
import { User } from "./User"
import { AdminMenu } from "./AdminMenu"
import { ActivitiesService } from "@/client"
import { cn } from "@/lib/utils"
import { useMultiEventSubscription, EventTypes } from "@/hooks/useEventBus"

const itemsBeforeActivities: Item[] = [
  { icon: Home, title: "Dashboard", path: "/" },
]

const itemsAfterActivities: Item[] = [
  { icon: Bot, title: "Agents", path: "/agents" },
  { icon: MessageSquare, title: "Sessions", path: "/sessions" },
  { icon: Key, title: "Credentials", path: "/credentials" },
]

function ActivitiesMenu() {
  const { isMobile, setOpenMobile } = useSidebar()
  const router = useRouterState()
  const currentPath = router.location.pathname
  const queryClient = useQueryClient()

  const { data: activityStats } = useQuery({
    queryKey: ["activity-stats"],
    queryFn: () => ActivitiesService.getActivityStats(),
    refetchInterval: 10000, // Refetch every 10 seconds
  })

  // Subscribe to WebSocket events for activities to update stats in real-time
  useMultiEventSubscription(
    [EventTypes.ACTIVITY_CREATED, EventTypes.ACTIVITY_UPDATED, EventTypes.ACTIVITY_DELETED],
    (event) => {
      console.log("[Sidebar] Received activity event:", event.type, event)
      // Invalidate stats to refetch with latest data
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

  return (
    <SidebarGroup>
      <SidebarGroupContent>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton tooltip="Activities" isActive={isActive} asChild>
              <RouterLink to="/activities" onClick={handleMenuClick}>
                <Bell
                  className={cn(
                    hasUnread && !hasActionRequired && "text-primary",
                    hasActionRequired && "text-destructive"
                  )}
                />
                <span>Activities</span>
              </RouterLink>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  )
}

function KnowledgeSourcesMenu() {
  const { isMobile, setOpenMobile } = useSidebar()
  const router = useRouterState()
  const currentPath = router.location.pathname

  const handleMenuClick = () => {
    if (isMobile) {
      setOpenMobile(false)
    }
  }

  const isActive = currentPath === "/knowledge-sources" || currentPath.startsWith("/knowledge-source/")

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <SidebarMenuButton tooltip="Knowledge Sources" isActive={isActive} asChild>
          <RouterLink to="/knowledge-sources" onClick={handleMenuClick}>
            <BookOpen />
            <span>Knowledge</span>
          </RouterLink>
        </SidebarMenuButton>
      </SidebarMenuItem>
    </SidebarMenu>
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
        <Main items={itemsBeforeActivities} />
        <ActivitiesMenu />
        <Main items={itemsAfterActivities} />
      </SidebarContent>
      <SidebarFooter>
        <WebSocketStatus />
        <SidebarWorkspaceSwitcher />
        <KnowledgeSourcesMenu />
        <SidebarAppearance />
        {currentUser?.is_superuser && <AdminMenu />}
        <User user={currentUser} />
      </SidebarFooter>
    </Sidebar>
  )
}

export default AppSidebar
