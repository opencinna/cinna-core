import { Link as RouterLink, useRouterState } from "@tanstack/react-router"
import { Shield, Users, Store } from "lucide-react"

import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

export function AdminMenu() {
  const { isMobile, setOpenMobile } = useSidebar()
  const router = useRouterState()
  const currentPath = router.location.pathname

  const handleMenuClick = () => {
    if (isMobile) {
      setOpenMobile(false)
    }
  }

  const isActive = currentPath.startsWith("/admin")

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <SidebarMenuButton tooltip="Admin" isActive={isActive}>
              <Shield className="size-4 text-muted-foreground" />
              <span>Admin</span>
            </SidebarMenuButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            side={isMobile ? "top" : "right"}
            align="end"
            className="w-(--radix-dropdown-menu-trigger-width) min-w-56"
          >
            <DropdownMenuItem asChild>
              <RouterLink to="/admin/users" onClick={handleMenuClick}>
                <Users className="mr-2 h-4 w-4" />
                Users
              </RouterLink>
            </DropdownMenuItem>
            <DropdownMenuItem asChild>
              <RouterLink to="/admin/marketplaces" onClick={handleMenuClick}>
                <Store className="mr-2 h-4 w-4" />
                Plugin Marketplaces
              </RouterLink>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  )
}
