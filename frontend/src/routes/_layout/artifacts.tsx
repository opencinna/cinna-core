import { createFileRoute } from "@tanstack/react-router"
import { useEffect } from "react"

import { ArtifactsList } from "@/components/Artifacts/ArtifactsList"
import { UploadArtifactDialog } from "@/components/Artifacts/UploadArtifactDialog"
import useWorkspace from "@/hooks/useWorkspace"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/artifacts")({
  component: Artifacts,
  head: () => ({
    meta: [{ title: `Artifacts - ${APP_NAME}` }],
  }),
})

function Artifacts() {
  const { setHeaderContent } = usePageHeader()
  const { activeWorkspaceId } = useWorkspace()

  useEffect(() => {
    setHeaderContent(
      <>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">Artifacts</h1>
          <p className="text-xs text-muted-foreground">
            Dashboards, reports and files your agents published
          </p>
        </div>
        <UploadArtifactDialog />
      </>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        {/* Remount on workspace change to reset the list's query state. */}
        <ArtifactsList key={activeWorkspaceId ?? "default"} />
      </div>
    </div>
  )
}
