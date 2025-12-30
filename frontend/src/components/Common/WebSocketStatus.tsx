import { useConnectionStatus } from "@/hooks/useEventBus"
import { cn } from "@/lib/utils"
import { Wifi, WifiOff } from "lucide-react"
import {
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"

export function WebSocketStatus() {
  const status = useConnectionStatus()

  const statusConfig = {
    connected: {
      color: "text-green-500",
      label: "Online",
      icon: Wifi,
    },
    connecting: {
      color: "text-yellow-500",
      label: "Connecting...",
      icon: Wifi,
    },
    disconnected: {
      color: "text-red-500",
      label: "Offline",
      icon: WifiOff,
    },
  }

  const config = statusConfig[status]
  const Icon = config.icon

  return (
    <SidebarMenuItem>
      <SidebarMenuButton tooltip={`Status: ${config.label}`}>
        <Icon className={cn("size-4", config.color)} />
        <span className={config.color}>{config.label}</span>
      </SidebarMenuButton>
    </SidebarMenuItem>
  )
}
