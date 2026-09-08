import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useEffect, useMemo, useRef, useState } from "react"

import {
  AdminAiProvidersService,
  AdminLlmProvidersService,
  type AIProviderConfigInput,
  type AIProviderPublic,
  type AIProviderUpdate,
} from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import {
  ProvisioningPolicyFields,
  type ProvisioningPolicyValue,
} from "@/components/Admin/AiProviders/ProvisioningPolicyFields"
import {
  adminConfigFields,
  useProviderAdapters,
} from "@/components/Admin/LlmProviders/useProviderAdapters"
import {
  AI_PROVIDERS_QUERY_KEY,
  aiProviderKindLabel,
  type AutoProvisionConflict,
  MANAGED_CREDENTIALS_QUERY_PREFIX,
  parseAutoProvisionConflict,
  parseAvailableModels,
  stripProviderPrefix,
} from "@/components/Admin/LlmProviders/providerTypes"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

interface EditProviderSheetProps {
  provider: AIProviderPublic
  open: boolean
  onOpenChange: (open: boolean) => void
}

/** Every editable value, flattened so it can be diffed against the snapshot. */
interface EditState extends ProvisioningPolicyValue {
  name: string
  base_url: string
  model: string
  /** The adapter's `admin_config_schema` fields, keyed as it names them. */
  config: Record<string, string>
}

function providerToState(provider: AIProviderPublic): EditState {
  const config: Record<string, string> = {}
  for (const [key, value] of Object.entries(provider.config ?? {})) {
    config[key] = value === null || value === undefined ? "" : String(value)
  }
  return {
    name: provider.name,
    base_url: provider.base_url ?? "",
    model: provider.model ?? "",
    config,
    auto_provision_roles: [...(provider.auto_provision_roles ?? [])],
    set_as_default: provider.set_as_default ?? false,
    set_user_sdk_defaults: provider.set_user_sdk_defaults ?? false,
    sdk_default_modes: [...(provider.sdk_default_modes ?? [])],
    model_override_conversation: provider.model_override_conversation ?? "",
    model_override_building: provider.model_override_building ?? "",
    default_model: provider.default_model ?? "",
    available_models: (provider.available_models ?? []).join("\n"),
    // `datetime` on the wire, `<input type="date">` here. The date half is the
    // whole of what this field means (a reminder), and sending it back as a
    // bare date is what the create route already accepts.
    expiry_notification_date: (provider.expiry_notification_date ?? "").slice(
      0,
      10,
    ),
  }
}

const sameSet = (a: string[], b: string[]) =>
  a.length === b.length &&
  [...a].sort().join("\u0000") === [...b].sort().join("\u0000")

/**
 * Edit a provider: its name, the shape of its key, and the rule for who gets
 * one — plus, before saving, the fact that the change reaches people who
 * already hold it.
 *
 * A right `Sheet` rather than a dialog, per §5: fourteen editable fields in
 * three titled sections is four times a form dialog's cap.
 *
 * **Not auto-saving**, unlike the Channels Configure sheet: neither of that
 * sheet's two conditions holds here. `null` does not mean "inherit" for any of
 * these fields, and every write re-applies the policy to every member (§5.3),
 * so a per-keystroke save would re-run that against live accounts on each
 * character typed. Explicit Save, and the submit is a **diff PATCH** against a
 * snapshot taken on open — which is what keeps the three-state
 * `model_override_*` contract intact (omitted = leave, `""` = clear and unpin,
 * a value = set) and stops a rename from re-asserting a policy the admin never
 * touched.
 *
 * **No Replace-key trigger of its own.** A `Dialog` opened from a `Sheet` is a
 * third disclosure level (R8); the row → dialog path is level 2, so the Key
 * section points at the row's menu instead.
 */
export function EditProviderSheet({
  provider,
  open,
  onOpenChange,
}: EditProviderSheetProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const {
    adapterFor,
    typeLabel,
    isPending: adaptersPending,
    isError: adaptersError,
  } = useProviderAdapters(open)

  const [state, setState] = useState<EditState>(() => providerToState(provider))
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [conflict, setConflict] = useState<AutoProvisionConflict | null>(null)
  // What the record looked like when the sheet opened. Every PATCH field is
  // diffed against this: the payload is absolute, the snapshot is stale the
  // moment it is taken (a signup auto-provisions, another admin edits, a
  // window-focus refetch lands), and resubmitting an untouched field asserts a
  // value this admin never chose — which for the policy fields is how a rename
  // comes back as a 409 for a slot conflict nobody introduced.
  const openedWith = useRef<EditState>(providerToState(provider))

  const adapter = adapterFor(provider.type)
  const isMinted = provider.kind === "minted"
  // One list for the renderer, the validator and the PATCH — see the same
  // comment in `ConnectProviderDialog`. Scoped to `minted` because
  // `admin_config_schema` is keyed on the type: without this a `fixed_key`
  // OpenAI provider is validated against a `project_id` input this sheet never
  // renders, and Save silently does nothing.
  const configFields = useMemo(
    () => (isMinted ? adminConfigFields(adapter) : []),
    [adapter, isMinted],
  )

  useEffect(() => {
    if (!open) return
    const seeded = providerToState(provider)
    openedWith.current = seeded
    setState(seeded)
    setErrors({})
    setConflict(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, provider.id])

  const patch = (next: Partial<EditState>) =>
    setState((current) => ({ ...current, ...next }))

  // The model pickers probe through the credential this provider owns, which
  // resolves the provider's own key server-side. A minted provider has no key
  // here to probe with — each member's is created at the vendor — so the
  // pickers are disabled rather than pointed at a request that would 400.
  const canProbe = !isMinted && !!provider.owned_credential_id
  const probeModels = () =>
    AdminLlmProvidersService.testManagedAiCredentialConnection({
      managedCredentialId: provider.owned_credential_id,
      requestBody: {
        type: provider.type,
        base_url: state.base_url.trim() || undefined,
      },
    })

  const updateMutation = useMutation({
    mutationFn: (body: AIProviderUpdate) =>
      AdminAiProvidersService.updateAiProvider({
        providerId: provider.id,
        requestBody: body,
      }),
    onSuccess: () => {
      showSuccessToast(`"${state.name.trim()}" updated.`)
      onOpenChange(false)
    },
    onError: (error) => {
      const detected = parseAutoProvisionConflict(error)
      if (detected) {
        setConflict(detected)
        return
      }
      handleError.call(showErrorToast, error as ApiError)
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: AI_PROVIDERS_QUERY_KEY })
      void queryClient.invalidateQueries({
        queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX,
      })
    },
  })

  const submit = () => {
    // A form whose field list came from a query cannot submit before the query
    // answers: `adapter` is undefined while it is pending or failed, so the
    // required Base URL / Model inputs are neither rendered nor validated and a
    // Save here would omit mandatory fields.
    if (!adaptersReady) return
    const nextErrors: Record<string, string> = {}
    if (state.name.trim() === "") nextErrors.name = "Name is required"
    if (adapter?.requires_base_url && state.base_url.trim() === "") {
      nextErrors.base_url = "Base URL is required for this type"
    }
    if (adapter?.requires_model && state.model.trim() === "") {
      nextErrors.model = "Model is required for this type"
    }
    for (const field of configFields) {
      if (field.required && (state.config[field.name] ?? "").trim() === "") {
        nextErrors[`config.${field.name}`] = `${field.label} is required`
      }
    }
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0) return

    const opened = openedWith.current
    const body: AIProviderUpdate = {}
    if (state.name.trim() !== opened.name.trim()) body.name = state.name.trim()
    // Trimmed on both sides. An untrimmed comparison fires a PATCH over a
    // stray trailing space, and a provider PATCH re-encrypts the fixed-key
    // envelope and re-applies the policy to every member.
    if (state.base_url.trim() !== opened.base_url.trim()) {
      body.base_url = state.base_url.trim() || null
    }
    if (state.model.trim() !== opened.model.trim()) {
      body.model = state.model.trim() || null
    }
    if (
      configFields.length > 0 &&
      configFields.some(
        (field) =>
          (state.config[field.name] ?? "") !== (opened.config[field.name] ?? ""),
      )
    ) {
      // Built from the schema's own field names, never from a hardcoded list:
      // the adapter says which keys its configuration has, and this sends
      // exactly those. A key it does not declare is a 422 rather than a silent
      // drop, which is why the shape must come from the schema.
      const config: Record<string, string | null> = {}
      for (const field of configFields) {
        config[field.name] = (state.config[field.name] ?? "").trim() || null
      }
      // Double cast, deliberately: see the same site in `ConnectProviderDialog`
      // — the keys come from the adapter's schema and must not be named here.
      body.config = config as unknown as AIProviderConfigInput
    }
    if (!sameSet(state.auto_provision_roles, opened.auto_provision_roles)) {
      body.auto_provision_roles = state.auto_provision_roles
    }
    if (state.set_as_default !== opened.set_as_default) {
      body.set_as_default = state.set_as_default
    }
    if (state.set_user_sdk_defaults !== opened.set_user_sdk_defaults) {
      body.set_user_sdk_defaults = state.set_user_sdk_defaults
    }
    if (!sameSet(state.sdk_default_modes, opened.sdk_default_modes)) {
      body.sdk_default_modes = state.sdk_default_modes
    }
    if (state.default_model !== opened.default_model) {
      // `""` is a real clear here: `update` writes the field whenever it is not
      // null, and the normaliser turns a blank into NULL.
      body.default_model = stripProviderPrefix(state.default_model)
    }
    // Diffed as parsed lists rather than as raw text: reflowing the textarea's
    // whitespace is not a change, and sending one writes through to members.
    const nextAvailable = parseAvailableModels(state.available_models)
    if (!sameSet(nextAvailable, parseAvailableModels(opened.available_models))) {
      body.available_models = nextAvailable
    }
    for (const field of [
      "model_override_conversation",
      "model_override_building",
    ] as const) {
      const next = stripProviderPrefix(state[field])
      if (next !== stripProviderPrefix(opened[field])) {
        // `""` clears the override *and* unpins every member still carrying the
        // dropped value; without sending it the clear would be cosmetic.
        body[field] = next
      }
    }
    if (
      state.expiry_notification_date !== opened.expiry_notification_date &&
      state.expiry_notification_date !== ""
    ) {
      // Omitted when blanked: `null` is "leave it alone" on this endpoint, so
      // there is nothing that clears a date. The field's helper says so.
      body.expiry_notification_date = state.expiry_notification_date
    }

    if (Object.keys(body).length === 0) {
      showSuccessToast("No changes to apply.")
      onOpenChange(false)
      return
    }
    updateMutation.mutate(body)
  }

  const isSaving = updateMutation.isPending
  const adaptersReady = !adaptersPending && !adaptersError
  const vendor = typeLabel(provider.type)

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Edit provider</SheetTitle>
          <SheetDescription>{provider.name}</SheetDescription>
        </SheetHeader>

        <div className="flex-1 space-y-6 overflow-y-auto px-4 pb-4">
          {/* ── 1. Details ─────────────────────────────────────────── */}
          <section className="space-y-3">
            <div>
              <h3 className="text-sm font-medium">Details</h3>
              <p className="text-xs text-muted-foreground">
                What this key source is called.
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor={`edit-${provider.id}-name`}>
                Name <span className="text-destructive">*</span>
              </Label>
              <Input
                id={`edit-${provider.id}-name`}
                value={state.name}
                disabled={isSaving}
                onChange={(event) => patch({ name: event.target.value })}
              />
              {errors.name && (
                <p className="text-xs text-destructive">{errors.name}</p>
              )}
            </div>
            {/* Prose, not two disabled controls: an immutable fact costs
                nothing to state and a greyed-out select explains nothing. */}
            <p className="text-xs text-muted-foreground">
              {vendor} · {aiProviderKindLabel(provider.kind)}. A provider's type
              and kind are fixed when it is connected.
            </p>
          </section>

          <Separator />

          {/* ── 2. Key ─────────────────────────────────────────────── */}
          <section className="space-y-3">
            <div>
              <h3 className="text-sm font-medium">Key</h3>
              <p className="text-xs text-muted-foreground">
                Where the key comes from, and what it needs to reach {vendor}.
              </p>
            </div>

            {adaptersPending && (
              <div className="space-y-2">
                <Skeleton className="h-4 w-24" />
                <Skeleton className="h-9 w-full" />
              </div>
            )}
            {adaptersError && (
              <p className="text-sm text-destructive">
                The type list could not be loaded, so this provider's key
                settings cannot be shown or saved. Refresh and try again.
              </p>
            )}

            {adapter?.requires_base_url && (
              <div className="space-y-2">
                <Label htmlFor={`edit-${provider.id}-base-url`}>
                  Base URL <span className="text-destructive">*</span>
                </Label>
                <Input
                  id={`edit-${provider.id}-base-url`}
                  placeholder="https://api.example.com/v1"
                  value={state.base_url}
                  disabled={isSaving}
                  onChange={(event) => patch({ base_url: event.target.value })}
                />
                {errors.base_url && (
                  <p className="text-xs text-destructive">{errors.base_url}</p>
                )}
              </div>
            )}
            {adapter?.requires_model && (
              <div className="space-y-2">
                <Label htmlFor={`edit-${provider.id}-model`}>
                  Model <span className="text-destructive">*</span>
                </Label>
                <Input
                  id={`edit-${provider.id}-model`}
                  placeholder="meta-llama/Llama-3-70b"
                  value={state.model}
                  disabled={isSaving}
                  onChange={(event) => patch({ model: event.target.value })}
                />
                {errors.model && (
                  <p className="text-xs text-destructive">{errors.model}</p>
                )}
              </div>
            )}

            {isMinted ? (
              <>
                {configFields.map((field) => (
                  <div key={field.name} className="space-y-2">
                    <Label htmlFor={`edit-${provider.id}-${field.name}`}>
                      {field.label}
                      {field.required && (
                        <span className="text-destructive"> *</span>
                      )}
                    </Label>
                    <Input
                      id={`edit-${provider.id}-${field.name}`}
                      inputMode={field.type === "integer" ? "numeric" : undefined}
                      value={state.config[field.name] ?? ""}
                      disabled={isSaving}
                      onChange={(event) =>
                        patch({
                          config: {
                            ...state.config,
                            [field.name]: event.target.value,
                          },
                        })
                      }
                    />
                    {field.help && (
                      <p className="text-xs text-muted-foreground">
                        {field.help}
                      </p>
                    )}
                    {errors[`config.${field.name}`] && (
                      <p className="text-xs text-destructive">
                        {errors[`config.${field.name}`]}
                      </p>
                    )}
                  </div>
                ))}
                <p className="text-xs text-muted-foreground">
                  Cinna only ever reads the project's monthly spend limit —
                  it never sets one, and never stores a copy of it.
                </p>
                <p className="text-xs text-muted-foreground">
                  Per-user keys are created and revoked one at a time, so there
                  is no single key here to replace.
                </p>
              </>
            ) : (
              <p className="text-xs text-muted-foreground">
                Replace the key itself from this provider's ⋯ menu —{" "}
                <strong>Replace key</strong>. It re-keys every member.
              </p>
            )}
          </section>

          <Separator />

          {/* ── 3. Provisioning policy ─────────────────────────────── */}
          <section className="space-y-3">
            <div>
              <h3 className="text-sm font-medium">Provisioning policy</h3>
              <p className="text-xs text-muted-foreground">
                Who automatically receives this key, and how it is wired for
                them.
              </p>
            </div>
            {/* Default variant, not destructive: this is a consequence of
                saving, not a warning about it — and §5.3 is the behaviour this
                sentence is the only reader of. */}
            <Alert>
              <AlertTitle>
                Changes here re-apply to everyone who already holds this key.
              </AlertTitle>
              <AlertDescription>
                Model and override changes are written through to every member;
                clearing an override unpins the members it pinned.
              </AlertDescription>
            </Alert>
            <ProvisioningPolicyFields
              value={state}
              onChange={patch}
              idPrefix={`edit-${provider.id}`}
              type={provider.type}
              conflict={conflict}
              probeModels={probeModels}
              probeDisabled={!canProbe || isSaving}
              storedOverrides={{
                conversation: provider.model_override_conversation ?? null,
                building: provider.model_override_building ?? null,
              }}
              showExpiry
              disabled={isSaving}
            />
          </section>
        </div>

        {/* `SheetFooter` stacks full-width by default, which is right for the
            one-button reference (`ChannelConfigSheet`) and wrong for a pair:
            two full-bleed buttons read as two primary actions. Rowed and
            right-aligned, as every Dialog footer in the product is. */}
        <SheetFooter className="flex-row justify-end">
          <Button
            type="button"
            variant="outline"
            disabled={isSaving}
            onClick={() => onOpenChange(false)}
          >
            Cancel
          </Button>
          <LoadingButton
            type="button"
            loading={isSaving}
            disabled={!adaptersReady}
            onClick={submit}
          >
            Save changes
          </LoadingButton>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}
