import { createFileRoute } from "@tanstack/react-router"
import { useEffect } from "react"
import { AgenticTeamSettings } from "@/components/AgenticTeams/AgenticTeamSettings"
import { HashTabs, type TabConfig } from "@/components/Common/HashTabs"
import { AICredentialsSettings } from "@/components/UserSettings/AICredentials"
import { AppDataTab } from "@/components/UserSettings/AppData/AppDataTab"
import { AppMcpServerCard } from "@/components/UserSettings/AppMcpServerCard"
import { IdentityContactsCard } from "@/components/UserSettings/Channels/IdentityContactsCard"
import { UserChannelsCard } from "@/components/UserSettings/Channels/UserChannelsCard"
import { DashboardSettings } from "@/components/UserSettings/DashboardSettings"
import DeleteAccount from "@/components/UserSettings/DeleteAccount"
import { DesktopSessionsCard } from "@/components/UserSettings/DesktopSessionsCard"
import { IdentityServerCard } from "@/components/UserSettings/IdentityServerCard"
import { LocalDevelopmentCard } from "@/components/UserSettings/LocalDevelopmentCard"
import { NotificationSettings } from "@/components/UserSettings/NotificationSettings"
import OAuthAccounts from "@/components/UserSettings/OAuthAccounts"
import PasswordCard from "@/components/UserSettings/PasswordCard"
import { SecurityTab } from "@/components/UserSettings/Security/SecurityTab"
import { SSHKeys } from "@/components/UserSettings/SSHKeys"
import { ThemeAndColors } from "@/components/UserSettings/ThemeAndColors"
import { UserDetailsSettings } from "@/components/UserSettings/UserDetailsSettings"
import UserInformation from "@/components/UserSettings/UserInformation"
import UserPreferences from "@/components/UserSettings/UserPreferences"
import { WorkspaceSettings } from "@/components/UserSettings/WorkspaceSettings"
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
        <p className="text-xs text-muted-foreground">
          Manage your account settings
        </p>
      </div>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  if (!currentUser) {
    return null
  }

  const tabs: TabConfig[] = [
    {
      value: "my-profile",
      title: "My profile",
      content: (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <UserInformation />
          <UserDetailsSettings />
          <UserPreferences />
          <NotificationSettings />
        </div>
      ),
    },
    {
      value: "security",
      title: "Security",
      content: (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <PasswordCard />
          <OAuthAccounts />
          <SecurityTab />
          <DesktopSessionsCard />
          <LocalDevelopmentCard />
        </div>
      ),
    },
    {
      value: "interface",
      title: "Interface",
      content: (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <ThemeAndColors />
          <WorkspaceSettings />
          <AgenticTeamSettings />
          <DashboardSettings />
        </div>
      ),
    },
    {
      value: "ai-credentials",
      title: "AI Credentials",
      content: <AICredentialsSettings />,
    },
    {
      value: "channels",
      title: "Channels",
      content: (
        // Row 1 is the two surfaces about reach — what may reach me, whom I
        // may reach; row 2 is the two endpoint/authoring surfaces. What each
        // card owns is documented in its own file header.
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 items-start">
          <UserChannelsCard />
          <IdentityContactsCard />
          <AppMcpServerCard />
          <IdentityServerCard />
        </div>
      ),
    },
    { value: "keys", title: "SSH Keys", content: <SSHKeys /> },
    { value: "app-data", title: "App Data", content: <AppDataTab /> },
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
