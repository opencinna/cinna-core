import { createFileRoute } from "@tanstack/react-router"
import { useEffect } from "react"

import { AICredentialsSettings } from "@/components/UserSettings/AICredentials"
import DeleteAccount from "@/components/UserSettings/DeleteAccount"
import { AppAgentRoutesCard } from "@/components/UserSettings/AppAgentRoutesCard"
import { DesktopSessionsCard } from "@/components/UserSettings/DesktopSessionsCard"
import { IdentityServerCard } from "@/components/UserSettings/IdentityServerCard"
import { MailServerSettings } from "@/components/UserSettings/MailServerSettings"
import { SSHKeys } from "@/components/UserSettings/SSHKeys"
import UserInformation from "@/components/UserSettings/UserInformation"
import { WorkspaceSettings } from "@/components/UserSettings/WorkspaceSettings"
import { AgenticTeamSettings } from "@/components/AgenticTeams/AgenticTeamSettings"
import { DashboardSettings } from "@/components/UserSettings/DashboardSettings"
import { HashTabs, TabConfig } from "@/components/Common/HashTabs"
import useAuth from "@/hooks/useAuth"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/settings")({
  component: UserSettings,
  head: () => ({
    meta: [
      {
        title: `Settings - ${APP_NAME}`,
      },
    ],
  }),
})

function UserSettings() {
  const { user: currentUser } = useAuth()
  const { setHeaderContent } = usePageHeader()

  useEffect(() => {
    setHeaderContent(
      <div className="min-w-0">
        <h1 className="text-lg font-semibold truncate">User Settings</h1>
        <p className="text-xs text-muted-foreground">Manage your account settings</p>
      </div>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  if (!currentUser) {
    return null
  }

  const tabs: TabConfig[] = [
    { value: "my-profile", title: "My profile", content: <UserInformation /> },
    {
      value: "interface",
      title: "Interface",
      content: (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <WorkspaceSettings />
          <AgenticTeamSettings />
          <DashboardSettings />
        </div>
      ),
    },
    { value: "ai-credentials", title: "AI Credentials", content: <AICredentialsSettings /> },
    {
      value: "channels",
      title: "Channels",
      content: (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <MailServerSettings />
          <AppAgentRoutesCard />
          <IdentityServerCard />
          <DesktopSessionsCard />
        </div>
      ),
    },
    { value: "keys", title: "SSH Keys", content: <SSHKeys /> },
    { value: "danger-zone", title: "Danger zone", content: <DeleteAccount /> },
  ]

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        <HashTabs tabs={tabs} defaultTab="my-profile" />
      </div>
    </div>
  )
}
