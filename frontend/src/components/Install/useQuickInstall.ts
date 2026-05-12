/**
 * useQuickInstall — install a bundle in one click from the catalog card.
 *
 * Fetches the same ``GET /catalog/{bundle_id}/install-context`` the full
 * install page uses, builds the default credential + AI selections (the
 * shape the install form would submit without any user changes), and
 * fires ``CatalogService.installBundle``. On success, hands off to
 * :func:`useBundleInstallNavigation` to either route the user to the
 * dashboard (gate "ready") or to the Credentials tab.
 */
import { useMutation } from "@tanstack/react-query"

import {
  CatalogService,
  type AICredentialSelections,
  type CatalogInstallContext,
  type InstallCredentialSelection,
} from "@/client"
import useCustomToast from "@/hooks/useCustomToast"

import { useBundleInstallNavigation } from "./useBundleInstallNavigation"

function buildDefaultCredentialsPayload(
  context: CatalogInstallContext,
): { [key: string]: InstallCredentialSelection } | null {
  const payload: { [key: string]: InstallCredentialSelection } = {}
  for (const spec of context.service_specs) {
    if (spec.provided_by === "publisher") {
      payload[spec.name] = { mode: "publisher_provides" }
    } else if (spec.suggested_credential_id) {
      payload[spec.name] = {
        mode: "use_existing",
        credential_id: spec.suggested_credential_id,
      }
    } else {
      payload[spec.name] = { mode: "skip" }
    }
  }
  return Object.keys(payload).length > 0 ? payload : null
}

function buildDefaultAISelections(
  context: CatalogInstallContext,
): AICredentialSelections | null {
  if (context.ai_provided_by_publisher) {
    return {
      conversation_credential_id: null,
      building_credential_id: null,
      use_publisher_ai: true,
    }
  }
  return null
}

export function useQuickInstall(bundleId: string) {
  const handlePostInstall = useBundleInstallNavigation()
  const { showErrorToast } = useCustomToast()

  return useMutation({
    mutationFn: async () => {
      const context = await CatalogService.getInstallContext({ bundleId })
      const credentials = buildDefaultCredentialsPayload(context)
      const ai_credential_selections = buildDefaultAISelections(context)
      return CatalogService.installBundle({
        bundleId,
        requestBody: { credentials, ai_credential_selections },
      })
    },
    onSuccess: handlePostInstall,
    onError: (e: unknown) => {
      const detail =
        (e as { body?: { detail?: string } }).body?.detail ?? "Install failed"
      showErrorToast(detail)
    },
  })
}
