import { createFileRoute, redirect } from "@tanstack/react-router"

// Redirect /tasks/:shortCode to the unified /task/:shortCode page
export const Route = createFileRoute("/_layout/tasks/$shortCode")({
  beforeLoad: ({ params }) => {
    throw redirect({
      to: "/task/$taskId",
      params: { taskId: params.shortCode },
    })
  },
  component: () => null,
})
