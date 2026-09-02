import { createFileRoute, redirect } from "@tanstack/react-router"
import { useEffect } from "react"

import { DisclaimerCard } from "@/components/Admin/DisclaimerCard"
import { LocalAgentKitCard } from "@/components/Admin/LocalAgentKitCard"
import { MailServersCard } from "@/components/Admin/MailServersCard"
import { AutoInstallAgentsCard } from "@/components/Admin/ServerChannels/AutoInstallAgentsCard"
import { ServerChannelsCard } from "@/components/Admin/ServerChannels/ServerChannelsCard"
import { ServerDebugToolsCard } from "@/components/Admin/ServerChannels/ServerDebugToolsCard"
import { HashTabs, type TabConfig } from "@/components/Common/HashTabs"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

export const Route = createFileRoute("/_layout/admin/server-configuration")({
  component: AdminServerConfiguration,
  head: () => ({
    meta: [
      {
        title: `Server Configuration - Admin - ${APP_NAME}`,
      },
    ],
  }),
  beforeLoad: async ({ context }) => {
    if (!isLoggedIn()) {
      throw redirect({ to: "/login" })
    }
    // context.user is populated by the _layout auth guard.
    const user = (context as any)?.user
    if (user && !user.is_superuser) {
      throw redirect({ to: "/" })
    }
  },
})

function AdminServerConfiguration() {
  const { setHeaderContent } = usePageHeader()
  const { user } = useAuth()

  useEffect(() => {
    setHeaderContent(
      <div className="min-w-0">
        <h1 className="text-lg font-semibold truncate">Server Configuration</h1>
        <p className="text-xs text-muted-foreground">
          Configure server-wide settings
        </p>
      </div>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  // Edge-case guard for a non-superuser that slipped past beforeLoad.
  if (user && !user.is_superuser) {
    return (
      <div className="p-6 text-center text-muted-foreground">
        You do not have permission to view this page.
      </div>
    )
  }

  const tabs: TabConfig[] = [
    {
      value: "interface",
      title: "Interface",
      content: (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <DisclaimerCard />
          <LocalAgentKitCard />
        </div>
      ),
    },
    {
      value: "channels",
      title: "Channels",
      content: (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <ServerChannelsCard />
          <AutoInstallAgentsCard />
          <ServerDebugToolsCard />
        </div>
      ),
    },
    {
      // A peer of Channels, not part of it: an email channel references a mail
      // server by id the way a Google Chat channel references its service
      // account, and the servers outlive any one channel.
      value: "mail-servers",
      title: "Mail Servers",
      content: (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <MailServersCard />
        </div>
      ),
    },
  ]

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl">
        <HashTabs tabs={tabs} />
      </div>
    </div>
  )
}
