import { createFileRoute, redirect } from "@tanstack/react-router"
import { useQuery } from "@tanstack/react-query"

import { AgentsService, DashboardsService } from "@/client"
import { isLoggedIn } from "@/hooks/useAuth"
import { useEventBusConnection } from "@/hooks/useEventBus"
import PendingItems from "@/components/Pending/PendingItems"
import { DashboardGrid } from "@/components/Dashboard/UserDashboards/DashboardGrid"

export const Route = createFileRoute("/dashboard-fullscreen/$dashboardId")({
  component: FullscreenDashboardPage,
  beforeLoad: async () => {
    if (!isLoggedIn()) {
      throw redirect({ to: "/login" })
    }
  },
  head: () => ({
    meta: [{ title: "Dashboard - Fullscreen" }],
  }),
})

function FullscreenDashboardPage() {
  const { dashboardId } = Route.useParams()
  useEventBusConnection()

  const { data: dashboard, isLoading: dashboardLoading } = useQuery({
    queryKey: ["userDashboard", dashboardId],
    queryFn: () => DashboardsService.getDashboard({ dashboardId }),
  })

  const { data: agentsData, isLoading: agentsLoading } = useQuery({
    queryKey: ["allAgents"],
    queryFn: () => AgentsService.readAgents({ limit: 200 }),
    enabled: !!dashboard,
  })

  if (dashboardLoading || agentsLoading) {
    return <PendingItems />
  }

  if (!dashboard) {
    return (
      <div className="flex items-center justify-center h-screen">
        <p className="text-muted-foreground">Dashboard not found</p>
      </div>
    )
  }

  const agents = agentsData?.data ?? []

  return (
    <div className="h-screen w-screen overflow-hidden bg-background">
      <DashboardGrid
        dashboard={dashboard}
        agents={agents}
        isEditMode={false}
        showAddBlock={false}
        onCloseAddBlock={() => {}}
        onRequestAddBlock={() => {}}
      />
    </div>
  )
}
