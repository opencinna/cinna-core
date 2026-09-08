import { ChevronDown, ChevronRight, ExternalLink } from "lucide-react"
import { useState } from "react"

import type { AICredentialTestResult, AICredentialType } from "@/client"
import { ModelOverrideField } from "@/components/Admin/LlmProviders/ModelOverrideField"
import {
  type AutoProvisionConflict,
  describeAutoProvisionConflict,
  modelOverrideField,
  providerModelsDocUrl,
  SDK_MODE_OPTIONS,
} from "@/components/Admin/LlmProviders/providerTypes"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { USER_ROLE_OPTIONS } from "@/utils/userRoles"

/**
 * Everything a provider decides about *who gets a key and how it is wired*.
 *
 * The values are exactly `AIProviderCreate`'s policy half, held as strings
 * where the form edits text, so a surface can diff them against what it opened
 * with.
 */
export interface ProvisioningPolicyValue {
  auto_provision_roles: string[]
  set_as_default: boolean
  set_user_sdk_defaults: boolean
  sdk_default_modes: string[]
  model_override_conversation: string
  model_override_building: string
  default_model: string
  /** The textarea's raw text; parsed with `parseAvailableModels` on submit. */
  available_models: string
  /** `yyyy-mm-dd`, or `""` for none. Offered on edit only. */
  expiry_notification_date: string
}

export const EMPTY_PROVISIONING_POLICY: ProvisioningPolicyValue = {
  auto_provision_roles: [],
  set_as_default: false,
  set_user_sdk_defaults: false,
  sdk_default_modes: [],
  model_override_conversation: "",
  model_override_building: "",
  default_model: "",
  available_models: "",
  expiry_notification_date: "",
}

interface ProvisioningPolicyFieldsProps {
  value: ProvisioningPolicyValue
  onChange: (patch: Partial<ProvisioningPolicyValue>) => void
  /** Unique per host instance; every control id is derived from it. */
  idPrefix: string
  /** The provider's vendor, for the vendor's own model-list link. */
  type: AICredentialType | undefined
  /** The 409 the server raises when another provider owns a claimed slot. */
  conflict: AutoProvisionConflict | null
  /** Fetch a live model list for the two override pickers. */
  probeModels: () => Promise<AICredentialTestResult>
  /** True when there is no key to probe with, or a save is in flight. */
  probeDisabled: boolean
  /** The per-mode overrides already stored, so a clear can say what it costs. */
  storedOverrides?: {
    conversation: string | null
    building: string | null
  }
  /** `expiry_notification_date` is an edit-time field only — see below. */
  showExpiry?: boolean
  disabled?: boolean
}

/**
 * The five blocks that say who receives this provider's key, shared verbatim
 * by the create wizard's last step and the edit sheet's last section.
 *
 * One component rather than two, because the two surfaces must not drift in
 * copy: the `set_as_default` helper carries the feature's headline
 * configuration ("off means an extra key"), and a create wizard that explains
 * it while the edit sheet does not is how an admin learns the rule once and
 * then cannot find it again.
 *
 * Controlled with `value`/`onChange` rather than bound to `react-hook-form`:
 * both hosts hold plain state, because the *other* half of each form — the
 * key section — is defined at runtime by the chosen adapter's
 * `admin_config_schema`, and a static zod resolver would have to restate a
 * field list the server had just sent. Validation is per field and per step in
 * the host, which is what the specification asks for; the mechanism carrying
 * it is the host's error map rather than `FormMessage`.
 *
 * `expiry_notification_date` is offered on edit only. At create time it
 * describes a key the admin has just pasted and has no sensible value yet.
 */
export function ProvisioningPolicyFields({
  value,
  onChange,
  idPrefix,
  type,
  conflict,
  probeModels,
  probeDisabled,
  storedOverrides,
  showExpiry = false,
  disabled,
}: ProvisioningPolicyFieldsProps) {
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const docUrl = providerModelsDocUrl(type)

  const toggleIn = (
    field: "auto_provision_roles" | "sdk_default_modes",
    entry: string,
    checked: boolean,
  ) => {
    const current = value[field]
    onChange({
      [field]: checked
        ? current.includes(entry)
          ? current
          : [...current, entry]
        : current.filter((item) => item !== entry),
    })
  }

  return (
    <div className="space-y-4">
      {/* 1 — the rule */}
      <div className="space-y-2">
        <Label className="text-sm font-medium">
          Auto-provision for new accounts
        </Label>
        <div className="flex flex-wrap gap-x-6 gap-y-2">
          {USER_ROLE_OPTIONS.map((role) => (
            <div key={role.value} className="flex items-center gap-2">
              <Checkbox
                id={`${idPrefix}-role-${role.value}`}
                disabled={disabled}
                checked={value.auto_provision_roles.includes(role.value)}
                onCheckedChange={(checked) =>
                  toggleIn("auto_provision_roles", role.value, checked === true)
                }
              />
              <Label
                htmlFor={`${idPrefix}-role-${role.value}`}
                className="text-sm font-normal"
              >
                {role.label}
              </Label>
            </div>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          Applied when an account is created. Changing someone's role later
          never grants or revokes a key — use <strong>Apply to existing
          users</strong> for accounts that already exist.
        </p>
        {conflict && (
          <Alert variant="destructive">
            <AlertTitle>Another provider owns that default</AlertTitle>
            <AlertDescription>
              {describeAutoProvisionConflict(conflict)}
            </AlertDescription>
          </Alert>
        )}
      </div>

      {/* 2 — the "extra key" switch (§9's headline configuration) */}
      <div className="flex items-start justify-between gap-4 rounded-md border p-3">
        <div className="min-w-0 space-y-0.5">
          <Label
            htmlFor={`${idPrefix}-set-default`}
            className="text-sm font-medium"
          >
            Make it their default key
          </Label>
          <p className="text-xs text-muted-foreground">
            Off means an extra key: everyone matching the roles above still
            receives it, it appears in their settings and in Desktop and is
            selectable per agent, and it displaces nobody's existing default.
          </p>
        </div>
        <Switch
          id={`${idPrefix}-set-default`}
          disabled={disabled}
          checked={value.set_as_default}
          onCheckedChange={(checked) => onChange({ set_as_default: checked })}
        />
      </div>

      {/* 3 — the SDK wiring switch, which gates block 4 */}
      <div className="flex items-start justify-between gap-4 rounded-md border p-3">
        <div className="min-w-0 space-y-0.5">
          <Label
            htmlFor={`${idPrefix}-sdk-defaults`}
            className="text-sm font-medium"
          >
            Wire it into their SDK defaults
          </Label>
          <p className="text-xs text-muted-foreground">
            Points each recipient's conversation and building defaults at this
            key, for the modes ticked below.
          </p>
        </div>
        <Switch
          id={`${idPrefix}-sdk-defaults`}
          disabled={disabled}
          checked={value.set_user_sdk_defaults}
          onCheckedChange={(checked) =>
            onChange({ set_user_sdk_defaults: checked })
          }
        />
      </div>

      {/* 4 — the modes, and each ticked mode's override. The override lives
          with the mode it belongs to rather than under Advanced: it has no
          meaning apart from it. */}
      {value.set_user_sdk_defaults && (
        <div className="space-y-4 rounded-md border p-3">
          <div className="space-y-2">
            <Label className="text-sm font-medium">Modes to wire</Label>
            <div className="flex flex-wrap gap-x-6 gap-y-2">
              {SDK_MODE_OPTIONS.map((option) => (
                <div key={option.value} className="flex items-center gap-2">
                  <Checkbox
                    id={`${idPrefix}-mode-${option.value}`}
                    disabled={disabled}
                    checked={value.sdk_default_modes.includes(option.value)}
                    onCheckedChange={(checked) =>
                      toggleIn(
                        "sdk_default_modes",
                        option.value,
                        checked === true,
                      )
                    }
                  />
                  <Label
                    htmlFor={`${idPrefix}-mode-${option.value}`}
                    className="text-sm font-normal"
                  >
                    {option.label}
                  </Label>
                </div>
              ))}
            </div>
          </div>

          {SDK_MODE_OPTIONS.filter((option) =>
            value.sdk_default_modes.includes(option.value),
          ).map((option) => {
            const field = modelOverrideField(option.value)
            return (
              <ModelOverrideField
                key={option.value}
                id={`${idPrefix}-override-${option.value}`}
                modeLabel={option.label}
                value={value[field]}
                onChange={(next) => onChange({ [field]: next })}
                storedValue={
                  option.value === "conversation"
                    ? (storedOverrides?.conversation ?? null)
                    : (storedOverrides?.building ?? null)
                }
                probeModels={probeModels}
                probeDisabled={probeDisabled}
                disabled={disabled}
              />
            )
          })}
        </div>
      )}

      {/* 5 — one Advanced disclosure, per §1's Create rule. */}
      <div className="rounded-md border">
        <button
          type="button"
          onClick={() => setAdvancedOpen((open) => !open)}
          aria-expanded={advancedOpen}
          className="flex w-full items-center gap-1.5 px-3 py-2 text-sm font-medium text-muted-foreground hover:text-foreground"
        >
          {advancedOpen ? (
            <ChevronDown className="h-3.5 w-3.5" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" />
          )}
          Advanced
        </button>
        {advancedOpen && (
          <div className="space-y-4 border-t p-3">
            <div className="space-y-2">
              <div className="flex items-center justify-between gap-2">
                <Label htmlFor={`${idPrefix}-default-model`}>
                  Default model
                </Label>
                {docUrl && (
                  <a
                    href={docUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground hover:underline"
                  >
                    View available models
                    <ExternalLink className="h-3 w-3" />
                  </a>
                )}
              </div>
              <Input
                id={`${idPrefix}-default-model`}
                placeholder="e.g. claude-sonnet-4-6"
                disabled={disabled}
                value={value.default_model}
                onChange={(event) =>
                  onChange({ default_model: event.target.value })
                }
              />
              <p className="text-xs text-muted-foreground">
                The model used by default with this key. Leave blank to use the
                platform default.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor={`${idPrefix}-available-models`}>
                Available models
              </Label>
              <Textarea
                id={`${idPrefix}-available-models`}
                rows={3}
                placeholder="One model id per line (or comma-separated)"
                disabled={disabled}
                value={value.available_models}
                onChange={(event) =>
                  onChange({ available_models: event.target.value })
                }
              />
              <p className="text-xs text-muted-foreground">
                Models offered for selection with this key. Leave empty to offer
                all auto-detected models.
              </p>
            </div>

            {showExpiry && (
              <div className="space-y-2">
                <Label htmlFor={`${idPrefix}-expiry`}>
                  Expiry notification date
                </Label>
                <Input
                  id={`${idPrefix}-expiry`}
                  type="date"
                  disabled={disabled}
                  value={value.expiry_notification_date}
                  onChange={(event) =>
                    onChange({ expiry_notification_date: event.target.value })
                  }
                />
                {/* The last sentence is the endpoint's contract, not caution:
                    `AIProvidersService.update` writes this field only when it
                    arrives non-null, and `null` is how "leave it alone" is
                    spelled — so there is no value this form can send that
                    removes a date. Saying so beats a blanked box that saves
                    green and changes nothing. */}
                <p className="text-xs text-muted-foreground">
                  When this key is due to be replaced. A reminder only — nothing
                  stops working on this date. A date can be set or moved here,
                  but not removed.
                </p>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
