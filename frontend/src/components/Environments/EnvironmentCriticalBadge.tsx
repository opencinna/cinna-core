import { AlertTriangle } from "lucide-react"
import type { AgentEnvironmentPublic } from "@/client"

/**
 * Amber "Action required" block surfaced when an environment is in a persisted
 * critical state: the container is running, but a post-start/post-rebuild step
 * (custom package install, credential/file sync, generic provisioning) failed.
 *
 * This is deliberately NOT routed through `EnvironmentStatusBadge` — the status
 * badge keeps showing the green "Running" state because the container IS up.
 * The critical state layers an amber warning on top, mirroring the
 * `ModelHealthBadge` orange-palette convention but rendered as a more prominent
 * full-width block (it carries an actionable "Show details" affordance).
 *
 * Renders `null` when the environment is not critical.
 */

// Maps the backend `critical_cause` codes to a short, human-readable line.
const CAUSE_LABELS: Record<string, string> = {
  package_install_failed: "Custom package install failed.",
  system_package_install_failed: "System package install failed.",
  file_sync_failed: "Workspace file sync failed.",
  credential_sync_failed: "Credential sync failed.",
  provisioning_failed: "A provisioning step failed.",
}

function causeLine(cause: string | null | undefined): string {
  if (cause && cause in CAUSE_LABELS) return CAUSE_LABELS[cause]
  return "A setup step did not finish."
}

interface EnvironmentCriticalBadgeProps {
  environment: AgentEnvironmentPublic
  /**
   * Opens the owner-gated action-logs modal. When omitted (e.g. the read-only
   * agent-user card, whose viewer is not the owner), the block is informational
   * only — the "Show details" button is hidden because the underlying
   * `GET /environments/{id}/action-logs` route would 403 for a non-owner.
   */
  onShowDetails?: () => void
}

export function EnvironmentCriticalBadge({
  environment,
  onShowDetails,
}: EnvironmentCriticalBadgeProps) {
  if (!environment.critical_state) return null

  return (
    <div
      role="status"
      className="rounded-md border border-orange-200 bg-orange-100 px-3 py-2 text-orange-800 dark:border-orange-900 dark:bg-orange-900 dark:text-orange-200"
    >
      <div className="flex items-start gap-2">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">Action required</p>
          <p className="mt-0.5 text-xs">
            The container is running, but setup did not finish. Your agent may
            not behave as expected until this is resolved.
          </p>
          <p className="mt-0.5 text-xs font-medium">
            {causeLine(environment.critical_cause)}
          </p>
          {onShowDetails && (
            <button
              type="button"
              onClick={onShowDetails}
              className="mt-1.5 text-xs font-semibold underline underline-offset-2 hover:no-underline"
            >
              Show details
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
