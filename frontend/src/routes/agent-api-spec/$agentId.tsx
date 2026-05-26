import { createFileRoute, redirect } from "@tanstack/react-router"
import { OpenApiSpecViewer } from "@/components/Agents/OpenApiSpecViewer"
import PendingItems from "@/components/Pending/PendingItems"
import { useAgentApiSpec } from "@/hooks/useAgentApi"
import { isLoggedIn } from "@/hooks/useAuth"

/**
 * Standalone, full-page viewer for a producer agent's harvested OpenAPI spec,
 * opened in its own browser tab from the "View Spec" buttons (producer
 * Integrations card and consumer connection credential).
 *
 * Auth: this is a normal SPA route, so it runs inside the same app shell and
 * reuses the JWT in localStorage (``OpenAPI.TOKEN``) — the spec is fetched
 * through the authenticated client exactly like every other request, no
 * separate token handling. ``beforeLoad`` bounces unauthenticated tabs to login.
 *
 * Rendering: a custom, read-only ``OpenApiSpecViewer`` (built from the app's
 * own primitives, themed to match) renders the spec object fetched here.
 */
export const Route = createFileRoute("/agent-api-spec/$agentId")({
  component: AgentApiSpecPage,
  beforeLoad: async () => {
    if (!isLoggedIn()) {
      throw redirect({ to: "/login" })
    }
  },
  head: () => ({
    meta: [{ title: "Agent REST API — Spec" }],
  }),
})

function AgentApiSpecPage() {
  const { agentId } = Route.useParams()

  const { data: spec, isLoading, isError } = useAgentApiSpec(agentId, true)

  if (isLoading) {
    return <PendingItems />
  }

  if (isError || !spec) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-background p-6">
        <div className="space-y-2 text-center">
          <p className="text-base font-medium">
            Could not load the OpenAPI spec
          </p>
          <p className="text-sm text-muted-foreground">
            The producer's API may be stopped or failing to build. Check the
            build status on the agent's Integrations tab and try again.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="h-screen w-screen overflow-auto bg-background">
      <OpenApiSpecViewer spec={spec} />
    </div>
  )
}
