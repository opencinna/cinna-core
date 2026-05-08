import { createFileRoute, Outlet } from "@tanstack/react-router"

export const Route = createFileRoute("/_layout/catalog/agents")({
  component: () => <Outlet />,
})
