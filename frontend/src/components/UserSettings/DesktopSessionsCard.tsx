/**
 * DesktopSessionsCard — "App Sessions", Settings › Security tab.
 *
 * Which native app clients (Cinna Desktop and Cinna Mobile) are signed in to
 * this account, when each last ran, and a way to cut one off. Both kinds share
 * the same `desktop_oauth_client` backing table, so one card lists them.
 *
 * A View surface (guidelines §1): the only mutation is the revoke, and it is
 * one confirm away. The card is a P5 preview — five rows and "Show all (N)" —
 * which is what removes anti-pattern A4: the card's height used to *be* the
 * session count, so an account with many devices ran the card past the viewport
 * while its grid neighbour ended halfway down.
 *
 * This card is also the only surface on which a session created by
 * `POST /cli/account/desktop-token` — a desktop that linked itself from a CLI
 * `account.json` rather than through a browser consent — is visible to the
 * account owner, which is why such rows are badged. See the backend's
 * `AccountCLIService.exchange_for_desktop_token`.
 */
import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { MonitorSmartphone } from "lucide-react"

import type { DesktopOAuthClientPublic } from "@/client"
import { DesktopAuthService } from "@/client"
import { PREVIEW_COUNT, PreviewList } from "@/components/Common/PreviewList"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { APP_SESSIONS_QUERY_KEY } from "@/utils/appSessions"
import { AllAppSessionsSheet } from "./AllAppSessionsSheet"
import { AppSessionRow } from "./AppSessionRow"

/** Stable identity so the sort below memoises while the query loads. */
const NO_CLIENTS: DesktopOAuthClientPublic[] = []

/** Sort key: last used, or nothing when the app has never run. */
function lastUsedTime(client: DesktopOAuthClientPublic): number | null {
  if (!client.last_used_at) return null
  const t = new Date(client.last_used_at).getTime()
  return Number.isNaN(t) ? null : t
}

function createdTime(client: DesktopOAuthClientPublic): number {
  const t = new Date(client.created_at).getTime()
  return Number.isNaN(t) ? 0 : t
}

export function DesktopSessionsCard() {
  const [isSheetOpen, setIsSheetOpen] = useState(false)

  const {
    data: clientsData,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: APP_SESSIONS_QUERY_KEY,
    queryFn: () => DesktopAuthService.listDesktopClients(),
  })

  const clients = clientsData ?? NO_CLIENTS

  // Most recently used first, apps that have never run last, ties broken by
  // when they connected. The endpoint returns the whole list unpaginated, so
  // this ordering — and the count on "Show all" — is the true one.
  const sortedClients = useMemo(
    () =>
      [...clients].sort((a, b) => {
        const aUsed = lastUsedTime(a)
        const bUsed = lastUsedTime(b)
        if (aUsed !== bUsed) {
          if (aUsed === null) return 1
          if (bUsed === null) return -1
          return bUsed - aUsed
        }
        return createdTime(b) - createdTime(a)
      }),
    [clients],
  )

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 min-w-0">
          <MonitorSmartphone className="h-5 w-5 shrink-0" />
          App Sessions
        </CardTitle>
        <CardDescription>
          Cinna Desktop and Cinna Mobile apps signed in to this account.
        </CardDescription>
      </CardHeader>

      {/* No header button: an app connects from the app, never from here, so
          there is no Create action for the P3 header slot to hold. */}
      <CardContent>
        <PreviewList
          items={sortedClients}
          previewCount={PREVIEW_COUNT}
          getKey={(client) => client.client_id}
          renderItem={(client) => <AppSessionRow client={client} />}
          isLoading={isLoading}
          isError={isError}
          error={error}
          onRetry={() => refetch()}
          errorFallback="Couldn't load app sessions"
          empty={
            <p className="text-sm text-muted-foreground">
              No apps are connected to this account yet.{" "}
              <Link to="/desktop" className="text-primary hover:underline">
                Get Cinna Desktop
              </Link>
              .
            </p>
          }
          onShowAll={() => setIsSheetOpen(true)}
        />
      </CardContent>

      <AllAppSessionsSheet
        clients={sortedClients}
        open={isSheetOpen}
        onOpenChange={setIsSheetOpen}
      />
    </Card>
  )
}
