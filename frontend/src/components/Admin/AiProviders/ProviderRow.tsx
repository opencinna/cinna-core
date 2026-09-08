import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  BadgeCheck,
  KeyRound,
  Pencil,
  Plus,
  RefreshCw,
  Star,
  Trash2,
  TriangleAlert,
  UsersRound,
} from "lucide-react"
import { useState } from "react"

import {
  AdminAiProvidersService,
  type AIProviderPublic,
  type AIProviderVerifyResult,
  type MembershipProvisioningStatus,
} from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import { ApplyProviderDialog } from "@/components/Admin/AiProviders/ApplyProviderDialog"
import { DeleteProviderDialog } from "@/components/Admin/AiProviders/DeleteProviderDialog"
import { EditProviderSheet } from "@/components/Admin/AiProviders/EditProviderSheet"
import { ReplaceProviderKeyDialog } from "@/components/Admin/AiProviders/ReplaceProviderKeyDialog"
import {
  AI_PROVIDERS_QUERY_KEY,
  aiProviderKindLabel,
  MANAGED_CREDENTIALS_QUERY_PREFIX,
  sdkModeLabel,
  knownSdkModes,
} from "@/components/Admin/LlmProviders/providerTypes"
import { ListRow, RowFlag, RowInfo } from "@/components/Common/ListRow"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { membershipStatusMeta } from "@/utils/keyProvisioning"
import { userRoleLabel } from "@/utils/userRoles"

interface ProviderRowProps {
  provider: AIProviderPublic
  /** The vendor's display name, resolved once by the tab from the adapters. */
  typeLabel: string
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return ""
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })
}

/**
 * Coarse verify failures, in the vocabulary of the admin reading them.
 *
 * The codes are the ones `ProviderAdminError` and `verify` actually raise. An
 * unmapped code still renders verbatim, which is more useful than "something
 * went wrong" — but every code the OpenAI provisioner can produce today has a
 * sentence, because a raw `project_not_capped` in an admin-facing toast is the
 * same defect as a raw skip reason.
 */
const VERIFY_ERROR_COPY: Record<string, string> = {
  invalid_admin_secret: "the provider rejected the administration key",
  no_project_configured: "no project is configured on this provider",
  project_not_found: "the configured project no longer exists at the provider",
  provider_unreachable: "the provider could not be reached",
  rate_limited: "the provider is rate-limiting this instance",
  organization_spend_limit_exceeded:
    "the organisation has already exceeded its spend limit",
  project_spend_limit_exceeded:
    "the project has already exceeded its spend limit",
  incomplete_external_ref: "the provider returned an incomplete key reference",
  no_secret_returned: "the provider returned no key",
  provider_error: "the provider returned an unexpected response",
}

/**
 * The Verify result, in words — and §9's spend-limit rule, applied.
 *
 * **`project_not_capped` is tested before `ok`, and that ordering is the whole
 * point.** An uncapped project comes back as `ok=False` with that code (
 * `ai_providers_service.py:1001-1013`), not as a success with
 * `spend_limit_enforcing=false` — so a plain `if (!result.ok)` first would send
 * this case to the generic failure branch and print the raw token, which is
 * exactly the sentence §9 exists to make sure an admin gets.
 *
 * `checked_spend_limit === false` means *the question does not apply* (a
 * `fixed_key` provider has no project whose cap could be read), not "no cap
 * configured". Rendering it as the latter would be the false negative the field
 * exists to prevent, so that branch says nothing about limits at all.
 *
 * `spend_limit_cents` is never rendered: §9 says Cinna never displays a limit.
 * The number is a live read of a value the vendor's console owns, and a number
 * on screen in Cinna reads as a Cinna setting whether or not it was stored.
 */
export function describeVerifyResult(
  result: AIProviderVerifyResult,
  typeLabel: string,
): { ok: boolean; message: string } {
  if (result.error === "project_not_capped") {
    // A blocker, not a success: no key is created in an uncapped project.
    return {
      ok: false,
      message: `The key works, but the project has no monthly spend limit. Set one in the ${typeLabel} console — keys are not created in an uncapped project.`,
    }
  }
  if (!result.ok) {
    const reason = result.error ? VERIFY_ERROR_COPY[result.error] : undefined
    return {
      ok: false,
      message: reason
        ? `Verification failed — ${reason}.`
        : `Verification failed${result.error ? ` — ${result.error}` : ""}.`,
    }
  }
  if (!result.checked_spend_limit) {
    return { ok: true, message: "The key works." }
  }
  return {
    ok: true,
    message: "The connection works and the project has a monthly spend limit.",
  }
}

/**
 * One provider, as a plain P3 row at full tab width.
 *
 * The row answers §6.2's three questions left to right: the dot says whether
 * the connection still works, the badge and the metadata line say what kind of
 * key source this is and who automatically gets one, and the flags carry the
 * wiring policy and the counts.
 *
 * The row owns its Verify mutation and all four overlays. That is the point of
 * the split rather than a side effect: pending is then per row (verifying one
 * provider does not freeze the others), and a `DropdownMenuItem` unmounts on
 * select, so a dialog trigger nested in the menu would take its pending state
 * with it. The menu items set state; the overlays read it.
 *
 * **`Replace key` is omitted for a minted provider, not disabled.** A disabled
 * item cannot carry the explanation, and the explanation — per-user keys are
 * created and revoked one at a time, so there is no single key to replace —
 * lives in the edit sheet's Key section instead. Omitting it also makes §8's
 * 400 unreachable from the UI rather than caught after the fact.
 */
export function ProviderRow({ provider, typeLabel }: ProviderRowProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [editOpen, setEditOpen] = useState(false)
  const [applyOpen, setApplyOpen] = useState(false)
  const [replaceOpen, setReplaceOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)

  const verifyMutation = useMutation({
    mutationFn: () =>
      AdminAiProvidersService.verifyAiProvider({ providerId: provider.id }),
    onSuccess: (result) => {
      const described = describeVerifyResult(result, typeLabel)
      if (described.ok) showSuccessToast(described.message)
      else showErrorToast(described.message)
    },
    onError: (error: ApiError) => handleError.call(showErrorToast, error),
    onSettled: () => {
      // The row's dot and its detail flag carry the durable answer; the toast
      // is the result feedback, which never stays (§6).
      void queryClient.invalidateQueries({ queryKey: AI_PROVIDERS_QUERY_KEY })
      void queryClient.invalidateQueries({
        queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX,
      })
    },
  })

  // Per row, because this observer belongs to this row. The hazard the shared
  // `Set<id>` in `CompanyAiCredentialsCard` exists for — one `useMutation`
  // serving N rows, whose `variables` describe only its latest call — does not
  // arise when the row owns the mutation.
  const isPending = verifyMutation.isPending

  const isMinted = provider.kind === "minted"
  const roles = provider.auto_provision_roles ?? []
  const summary = provider.key_state_summary ?? {}
  const failedCount = summary.failed ?? 0
  const memberCount = provider.member_count ?? 0

  const status = provider.last_verify_error
    ? ({ tone: "error", label: "Last check failed" } as const)
    : provider.last_verified_at
      ? ({
          tone: "on",
          label: `Verified ${formatDateTime(provider.last_verified_at)}`,
        } as const)
      : ({ tone: "off", label: "Never verified" } as const)

  const rule =
    roles.length > 0
      ? `Automatic for ${roles.map(userRoleLabel).join(", ")}`
      : "Not automatic"

  const wiredModes = provider.set_user_sdk_defaults
    ? knownSdkModes(provider.sdk_default_modes)
    : []
  const defaultFlagLabel = provider.set_as_default
    ? `Becomes each recipient's default key${
        wiredModes.length > 0
          ? `, and wires their SDK defaults for ${wiredModes.map(sdkModeLabel).join(", ")}`
          : ""
      }`
    : "Extra key — granted in addition to what they already have, and displaces nobody's default."

  // `not_applicable` is every member of a fixed-key provider, so including it
  // would print the member count twice ("3 members · 3 shared key").
  const keyStates = Object.entries(summary)
    .filter(([state, count]) => count > 0 && state !== "not_applicable")
    .map(
      ([state, count]) =>
        `${count} ${membershipStatusMeta(state as MembershipProvisioningStatus).adminLabel.toLowerCase()}`,
    )
    .join(" · ")

  return (
    <ListRow
      status={status}
      icon={
        <span className="flex h-6 w-6 items-center justify-center rounded-md bg-muted text-muted-foreground">
          {isMinted ? (
            <UsersRound className="h-3.5 w-3.5" />
          ) : (
            <KeyRound className="h-3.5 w-3.5" />
          )}
        </span>
      }
      title={provider.name}
      badges={
        <Badge variant="outline" className="h-5 shrink-0">
          {aiProviderKindLabel(provider.kind)}
        </Badge>
      }
      meta={`${typeLabel} · ${rule}`}
      flags={
        <>
          {roles.length > 0 && (
            <RowFlag
              icon={provider.set_as_default ? Star : Plus}
              label={defaultFlagLabel}
            />
          )}
          {failedCount > 0 && (
            <RowFlag
              icon={TriangleAlert}
              tone="error"
              label={`${failedCount} members' keys failed to create. Open the credential to retry.`}
            />
          )}
          <RowInfo
            facts={[
              `${memberCount} ${memberCount === 1 ? "member" : "members"}`,
              keyStates,
              provider.config?.organization_id &&
                `Organisation ${provider.config.organization_id}`,
              provider.config?.project_id &&
                `Project ${provider.config.project_id}`,
              provider.last_verified_at
                ? `Last verified ${formatDateTime(provider.last_verified_at)}`
                : "Never verified",
              provider.last_verify_error,
            ]}
          />
        </>
      }
    >
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            disabled={isPending}
            onClick={() => verifyMutation.mutate()}
            aria-label={`Verify ${provider.name}`}
          >
            <BadgeCheck className="h-3.5 w-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          Verify
        </TooltipContent>
      </Tooltip>

      <RowActionsMenu
        label={`the provider ${provider.name}`}
        disabled={isPending}
      >
        <DropdownMenuItem
          onSelect={(event) => {
            event.preventDefault()
            setEditOpen(true)
          }}
        >
          <Pencil />
          Edit provider
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={(event) => {
            event.preventDefault()
            setApplyOpen(true)
          }}
        >
          <UsersRound />
          Apply to existing users
        </DropdownMenuItem>
        {!isMinted && (
          <DropdownMenuItem
            onSelect={(event) => {
              event.preventDefault()
              setReplaceOpen(true)
            }}
          >
            <RefreshCw />
            Replace key
          </DropdownMenuItem>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem
          variant="destructive"
          onSelect={(event) => {
            event.preventDefault()
            setDeleteOpen(true)
          }}
        >
          <Trash2 />
          Delete provider
        </DropdownMenuItem>
      </RowActionsMenu>

      {/* Mounted only while open: a resident instance would re-seed its form
          from a background refetch and lose an in-progress edit — and the
          delete dialog fires its impact probe on mount. */}
      {editOpen && (
        <EditProviderSheet
          provider={provider}
          open
          onOpenChange={setEditOpen}
        />
      )}
      {applyOpen && (
        <ApplyProviderDialog
          provider={provider}
          typeLabel={typeLabel}
          open
          onOpenChange={setApplyOpen}
        />
      )}
      {replaceOpen && !isMinted && (
        <ReplaceProviderKeyDialog
          provider={provider}
          typeLabel={typeLabel}
          open
          onOpenChange={setReplaceOpen}
        />
      )}
      {deleteOpen && (
        <DeleteProviderDialog
          provider={provider}
          typeLabel={typeLabel}
          open
          onOpenChange={setDeleteOpen}
        />
      )}
    </ListRow>
  )
}
