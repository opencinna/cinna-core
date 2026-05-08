/**
 * SetupNeededBanner — pre-LLM gate banner on the install/agent detail page.
 *
 * Calls ``GET /agents/{agentId}/setup-status`` and renders a warning banner
 * when the install is not ``ready``. The two non-ready statuses get distinct
 * copy:
 *   - ``needs_setup`` — installer-fillable placeholders are empty, primary
 *     action navigates to the setup-credentials page.
 *   - ``publisher_broken`` — publisher credentials are missing/unshared, no
 *     installer-side action will help; we tell the user to contact the
 *     publisher.
 *
 * Subscribes to the three Phase 4 install-setup WebSocket events
 * (``INSTALL_SETUP_REQUIRED``, ``INSTALL_SETUP_COMPLETED``,
 * ``PUBLISHER_CREDENTIAL_BROKEN``) and invalidates the setup-status query
 * so the banner appears/disappears in real time without a manual refresh.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { AlertTriangle, ShieldAlert } from "lucide-react"

import { InstallsService, type SetupStatusMissingItem } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { useMultiEventSubscription, EventTypes } from "@/hooks/useEventBus"

interface SetupNeededBannerProps {
  agentId: string
}

function formatMissingNames(items: SetupStatusMissingItem[]): string {
  if (items.length === 0) return ""
  const names = items.map((m) => m.spec_name)
  if (names.length === 1) return names[0]
  if (names.length === 2) return `${names[0]} and ${names[1]}`
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`
}

export function SetupNeededBanner({ agentId }: SetupNeededBannerProps) {
  const queryClient = useQueryClient()

  const { data: status, isLoading } = useQuery({
    queryKey: ["agent", agentId, "setup-status"],
    queryFn: () => InstallsService.getSetupStatus({ agentId }),
    enabled: !!agentId,
  })

  // Real-time refresh on the three Phase 4 install-setup events.
  // The query is invalidated for any of them — the banner's own logic
  // decides what to render based on the freshly fetched status.
  useMultiEventSubscription(
    [
      EventTypes.INSTALL_SETUP_REQUIRED,
      EventTypes.INSTALL_SETUP_COMPLETED,
      EventTypes.PUBLISHER_CREDENTIAL_BROKEN,
    ],
    (event) => {
      // Only invalidate when this event is for our install (model_id is the
      // agent/install id). Fall back to invalidating on any match if the
      // backend omits model_id.
      if (!event.model_id || event.model_id === agentId) {
        queryClient.invalidateQueries({
          queryKey: ["agent", agentId, "setup-status"],
        })
      }
    },
  )

  if (isLoading || !status) return null
  if (status.status === "ready") return null

  if (status.status === "publisher_broken") {
    const missingDesc = formatMissingNames(status.missing)
    return (
      <Alert variant="destructive" className="mb-4">
        <ShieldAlert className="h-4 w-4" />
        <AlertTitle>Publisher credentials unavailable</AlertTitle>
        <AlertDescription>
          {missingDesc
            ? `The publisher credentials for ${missingDesc} are no longer available for this install. `
            : "Publisher credentials for this install are no longer available. "}
          Contact the publisher to restore access, or replace them with your own
          credentials.
        </AlertDescription>
      </Alert>
    )
  }

  // status === "needs_setup"
  const missingDesc = formatMissingNames(status.missing)
  return (
    <Alert className="mb-4 border-amber-500/50 bg-amber-50 text-amber-900 dark:border-amber-400/40 dark:bg-amber-950/40 dark:text-amber-100 [&>svg]:text-amber-600 dark:[&>svg]:text-amber-300">
      <AlertTriangle className="h-4 w-4" />
      <AlertTitle>Setup needed before this agent can run</AlertTitle>
      <AlertDescription className="flex flex-col gap-3">
        <span>
          {missingDesc
            ? `Fill in the missing credential${
                status.missing.length === 1 ? "" : "s"
              } for ${missingDesc} to start using this agent.`
            : "Some credentials need to be filled in before this agent can start."}
        </span>
        <div>
          <Button asChild size="sm">
            <Link
              to="/agent/$agentId/setup-credentials"
              params={{ agentId }}
            >
              Open setup
            </Link>
          </Button>
        </div>
      </AlertDescription>
    </Alert>
  )
}

export default SetupNeededBanner
