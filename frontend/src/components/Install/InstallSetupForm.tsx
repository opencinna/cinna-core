/**
 * InstallSetupForm — right-column form on the install page.
 *
 * Orchestrates the AI credential section + per-spec service credential
 * accordion items + the single primary "Install" button. On submit it
 * builds the new ``InstallCredentialSelection`` payload (per spec) and
 * fires ``CatalogService.installBundle``.
 *
 * The button area swaps to a small env-progress display while the
 * mutation is in flight, mirroring the previous wizard's confirm step.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useState } from "react"

import {
  CatalogService,
  InstallsService,
  type AICredentialSelections,
  type CatalogInstallContext,
  type InstallContextSpec,
  type InstallCredentialSelection,
} from "@/client"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import useCustomToast from "@/hooks/useCustomToast"

import { InstallAICredentialSection } from "./InstallAICredentialSection"
import {
  InstallServiceCredentialItem,
  type ServiceCredentialChoice,
} from "./InstallServiceCredentialItem"

interface InstallSetupFormProps {
  context: CatalogInstallContext
}

/**
 * Map an :class:`InstallContextSpec` to the initial radio state for the
 * accordion item.
 */
function initialChoiceForSpec(
  spec: InstallContextSpec,
): ServiceCredentialChoice {
  if (spec.provided_by === "publisher") {
    return { mode: "publisher_provides" }
  }
  // PBU and PBT specs share the same default: if the install-context
  // matcher surfaced an existing credential of the same type, prefer
  // reusing it (handles the "uninstall → reinstall" scenario without
  // recreating duplicate credentials). Otherwise PBT falls back to
  // template materialisation and PBU to a placeholder — both routed
  // through "skip" since the backend branches on provided_by.
  if (spec.suggested_credential_id) {
    return { mode: "use_suggested" }
  }
  return { mode: "skip" }
}

export function InstallSetupForm({ context }: InstallSetupFormProps) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { bundle, ai_provided_by_publisher, service_specs } = context

  const [serviceChoices, setServiceChoices] = useState<
    Record<string, ServiceCredentialChoice>
  >(() => {
    const init: Record<string, ServiceCredentialChoice> = {}
    for (const spec of service_specs) {
      init[spec.name] = initialChoiceForSpec(spec)
    }
    return init
  })
  const [aiSelections, setAISelections] = useState<AICredentialSelections>({
    conversation_credential_id: null,
    building_credential_id: null,
    use_publisher_ai: ai_provided_by_publisher,
  })

  const updateChoice = (
    specName: string,
    next: ServiceCredentialChoice,
  ) => {
    setServiceChoices((prev) => ({ ...prev, [specName]: next }))
  }

  const installMutation = useMutation({
    mutationFn: () =>
      CatalogService.installBundle({
        bundleId: bundle.bundle_id,
        requestBody: {
          credentials: buildCredentialsPayload(),
          ai_credential_selections: ai_provided_by_publisher
            ? { ...aiSelections, use_publisher_ai: true }
            : aiSelections.conversation_credential_id ||
                aiSelections.building_credential_id
              ? aiSelections
              : null,
        },
      }),
    onSuccess: async (install) => {
      queryClient.invalidateQueries({ queryKey: ["agents"] })
      queryClient.invalidateQueries({ queryKey: ["catalog"] })

      // Decide where to send the installer based on whether the runtime
      // gate would let this agent chat right now. A "ready" install can
      // skip the Credentials tab entirely and land on the dashboard with
      // its pill pre-selected — a "chat with it now" experience for the
      // no-setup case (e.g. publisher-shared AI + publisher-shared
      // service credentials, or every spec auto-matched to an existing
      // user credential).
      //
      // Anything other than "ready" (needs_setup, publisher_broken, or a
      // setup-status call that errored) falls back to the existing
      // Credentials-tab redirect so the SetupNeededBanner can guide the
      // installer through the missing fields.
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
    },
    onError: (e: unknown) => {
      const detail =
        (e as { body?: { detail?: string } }).body?.detail ?? "Install failed"
      showErrorToast(detail)
    },
  })

  /**
   * Convert the in-memory radio + dropdown state into the new
   * :class:`InstallCredentialSelection` payload (one entry per spec).
   *
   * The frontend always emits the new shape — the backend's legacy
   * shim is for non-Cinna API clients only.
   */
  function buildCredentialsPayload(): {
    [key: string]: InstallCredentialSelection
  } | null {
    const payload: { [key: string]: InstallCredentialSelection } = {}
    for (const spec of service_specs) {
      const choice = serviceChoices[spec.name] ?? initialChoiceForSpec(spec)
      if (choice.mode === "publisher_provides") {
        payload[spec.name] = { mode: "publisher_provides" }
        continue
      }
      if (
        choice.mode === "use_suggested" &&
        spec.suggested_credential_id
      ) {
        payload[spec.name] = {
          mode: "use_existing",
          credential_id: spec.suggested_credential_id,
        }
        continue
      }
      if (choice.mode === "use_existing") {
        payload[spec.name] = {
          mode: "use_existing",
          credential_id: choice.credential_id,
        }
        continue
      }
      // "skip" → backend treats as placeholder.
      payload[spec.name] = { mode: "skip" }
    }
    return Object.keys(payload).length > 0 ? payload : null
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Service credentials</CardTitle>
          <CardDescription>
            {service_specs.length === 0
              ? "This bundle does not require any service credentials."
              : "Pick how each integration credential should be supplied. " +
                "You can always set up missing ones later."}
          </CardDescription>
        </CardHeader>
        {service_specs.length > 0 && (
          <CardContent className="space-y-2">
            {service_specs.map((spec) => (
              <InstallServiceCredentialItem
                key={spec.name}
                spec={spec}
                choice={
                  serviceChoices[spec.name] ?? initialChoiceForSpec(spec)
                }
                onChange={(next) => updateChoice(spec.name, next)}
              />
            ))}
          </CardContent>
        )}
      </Card>

      <InstallAICredentialSection
        aiProvidedByPublisher={ai_provided_by_publisher}
        aiPublisherSummaries={context.ai_publisher_credential_summaries}
        selections={aiSelections}
        onChange={setAISelections}
      />

      <div className="flex justify-end pt-2">
        {installMutation.isPending ? (
          <div className="flex items-center gap-3 text-sm text-muted-foreground">
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-primary" />
            Provisioning environment…
          </div>
        ) : (
          <Button
            size="lg"
            onClick={() => installMutation.mutate()}
            disabled={installMutation.isPending}
          >
            Install
          </Button>
        )}
      </div>
    </div>
  )
}
