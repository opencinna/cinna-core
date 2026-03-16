import { createFileRoute } from "@tanstack/react-router"
import { useEffect } from "react"

import { AICredentialsSettings } from "@/components/UserSettings/AICredentials"
import ChangePassword from "@/components/UserSettings/ChangePassword"
import DeleteAccount from "@/components/UserSettings/DeleteAccount"
import { GeneralAssistantSettings } from "@/components/UserSettings/GeneralAssistantSettings"
import OAuthAccounts from "@/components/UserSettings/OAuthAccounts"
import SetPassword from "@/components/UserSettings/SetPassword"
import { MailServerSettings } from "@/components/UserSettings/MailServerSettings"
import { SSHKeys } from "@/components/UserSettings/SSHKeys"
import UserInformation from "@/components/UserSettings/UserInformation"
import { WorkspaceSettings } from "@/components/UserSettings/WorkspaceSettings"
import { HashTabs, TabConfig } from "@/components/Common/HashTabs"
import useAuth from "@/hooks/useAuth"
import { usePageHeader } from "@/routes/_layout"

export const Route = createFileRoute("/_layout/settings")({
  component: UserSettings,
  head: () => ({
    meta: [
      {
        title: "Settings - Workflow Runner",
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

  // Build tabs based on user state
  const allTabs: TabConfig[] = [
    { value: "my-profile", title: "My profile", content: <UserInformation /> },
    { value: "password", title: "Password", content: <ChangePassword /> },
    { value: "set-password", title: "Set Password", content: <SetPassword /> },
    { value: "oauth", title: "Connected Accounts", content: <OAuthAccounts /> },
    { value: "interface", title: "Interface", content: <WorkspaceSettings /> },
    { value: "ai-credentials", title: "AI Credentials", content: <AICredentialsSettings /> },
    { value: "general-assistant", title: "General Assistant", content: <GeneralAssistantSettings /> },
    { value: "mail-servers", title: "Mail Servers", content: <MailServerSettings /> },
    { value: "keys", title: "SSH Keys", content: <SSHKeys /> },
    { value: "danger-zone", title: "Danger zone", content: <DeleteAccount /> },
  ]

  // Filter tabs based on user state
  let finalTabs = allTabs

  // Hide "Set Password" tab if user already has password
  if (currentUser && 'has_password' in currentUser && currentUser.has_password) {
    finalTabs = finalTabs.filter((tab) => tab.value !== "set-password")
  } else {
    // Hide "Change Password" tab if no password set
    finalTabs = finalTabs.filter((tab) => tab.value !== "password")
  }

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        <HashTabs tabs={finalTabs} defaultTab="my-profile" />
      </div>
    </div>
  )
}
