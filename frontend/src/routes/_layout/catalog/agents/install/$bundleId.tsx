import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { ArrowLeft } from "lucide-react"
import { useEffect } from "react"

import { InstallPage } from "@/components/Install/InstallPage"
import { useInstallContext } from "@/components/Install/useInstallContext"
import PendingItems from "@/components/Pending/PendingItems"
import { Button } from "@/components/ui/button"
import { useNavigationHistory } from "@/hooks/useNavigationHistory"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/catalog/agents/install/$bundleId")({
  component: InstallRouteComponent,
  head: () => ({
    meta: [{ title: `Install - ${APP_NAME}` }],
  }),
})

function InstallRouteComponent() {
  const { bundleId } = Route.useParams()
  const { setHeaderContent } = usePageHeader()
  const { goBack } = useNavigationHistory()
  const navigate = useNavigate()

  const { data: context, isLoading, error } = useInstallContext(bundleId)
  const entry = context?.bundle

  useEffect(() => {
    setHeaderContent(
      <div className="flex items-center gap-3 min-w-0">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => goBack("/catalog")}
          className="shrink-0"
        >
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div className="min-w-0">
          <h1 className="text-base font-semibold truncate">Install bundle</h1>
          <p className="text-xs text-muted-foreground truncate">
            {entry?.display_name ?? bundleId}
          </p>
        </div>
      </div>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, entry?.display_name, bundleId, goBack])

  // If the user already has an install, redirect them to the install detail.
  useEffect(() => {
    if (entry?.is_installed && entry.user_install_id) {
      navigate({
        to: "/agent/$agentId",
        params: { agentId: entry.user_install_id },
        replace: true,
      })
    }
  }, [entry, navigate])

  if (isLoading) {
    return <PendingItems />
  }

  if (error || !context) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">
          Bundle not found or not accessible.
        </p>
      </div>
    )
  }

  if (context.bundle.latest_revision_id === null) {
    return (
      <div className="p-6 md:p-8 mx-auto max-w-3xl">
        <p className="text-sm text-muted-foreground">
          This bundle has no published revisions yet — installation isn't
          possible until the publisher releases at least one revision.
        </p>
      </div>
    )
  }

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-6xl">
        <InstallPage context={context} />
      </div>
    </div>
  )
}
