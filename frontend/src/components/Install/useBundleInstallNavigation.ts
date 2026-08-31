/**
 * useBundleInstallNavigation — post-install side-effects shared between
 * the full install page and the catalog's Quick Install button.
 *
 * Invalidates the relevant queries, checks the runtime gate via
 * ``getSetupStatus`` and navigates the user either to the dashboard
 * (when the install is "ready" — they can chat with it immediately) or
 * to the install's Credentials tab (any other status).
 */
import { useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"

import { InstallsService, type AgentPublic } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"

export function useBundleInstallNavigation() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showSuccessToast } = useCustomToast()

  return async (install: AgentPublic) => {
    queryClient.invalidateQueries({ queryKey: ["agents"] })
    queryClient.invalidateQueries({ queryKey: ["catalog"] })

    let isReady = false
    try {
      const status = await InstallsService.getSetupStatus({
        agentId: install.id,
      })
      isReady = status.status === "ready"
    } catch {
      isReady = false
    }

    if (isReady) {
      showSuccessToast(
        `${install.name} installed — you can chat with it now.`,
      )
      navigate({
        to: "/",
        search: { selectAgentId: install.id },
      })
      return
    }

    showSuccessToast(`Installed ${install.name}`)
    navigate({
      to: "/agent/$agentId",
      params: { agentId: install.id },
      hash: "credentials",
    })
  }
}
