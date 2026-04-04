import { createFileRoute } from "@tanstack/react-router"
import { ManageDashboardsPage } from "@/components/Dashboard/UserDashboards/ManageDashboardsPage"

export const Route = createFileRoute("/_layout/dashboards/")({
  component: ManageDashboardsPage,
  head: () => ({
    meta: [
      {
        title: "Manage Dashboards - Workflow Runner",
      },
    ],
  }),
})
