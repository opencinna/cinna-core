import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Plus } from "lucide-react"
import { useEffect, useId, useMemo, useRef, useState } from "react"

import {
  AdminAiProvidersService,
  AdminLlmProvidersService,
  type AICredentialType,
  type AIProviderConfigInput,
  type AIProviderCreate,
  type AIProviderKind,
} from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import {
  EMPTY_PROVISIONING_POLICY,
  ProvisioningPolicyFields,
  type ProvisioningPolicyValue,
} from "@/components/Admin/AiProviders/ProvisioningPolicyFields"
import {
  adminConfigFields,
  useProviderAdapters,
} from "@/components/Admin/LlmProviders/useProviderAdapters"
import {
  AI_PROVIDER_KINDS,
  AI_PROVIDERS_QUERY_KEY,
  type AutoProvisionConflict,
  MANAGED_CREDENTIALS_QUERY_PREFIX,
  parseAutoProvisionConflict,
  parseAvailableModels,
  stripProviderPrefix,
} from "@/components/Admin/LlmProviders/providerTypes"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import { handleError } from "@/utils"

type Step = 1 | 2 | 3

interface CreateState extends ProvisioningPolicyValue {
  name: string
  secret: string
  base_url: string
  model: string
  config: Record<string, string>
}

const EMPTY_STATE: CreateState = {
  ...EMPTY_PROVISIONING_POLICY,
  name: "",
  secret: "",
  base_url: "",
  model: "",
  config: {},
}

/**
 * Connect a key source, and say who automatically gets one.
 *
 * A three-step wizard rather than one dialog, and a **separate surface from
 * editing**, for the reason §1 gives: create and edit do not share fields.
 * Create decides `kind`, `type` and the secret, none of which `AIProviderUpdate`
 * accepts — `secret` is absent there on purpose, because replacing a key is
 * `POST /{id}/rotate-key` and a quietly-ignored `secret` would look exactly like
 * the rotation that endpoint exists to perform.
 *
 * Step 1 is the **type**, as a pill picker, because the form's shape depends on
 * it (`requires_base_url`, `requires_model`, `admin_config_schema`,
 * `supports_minting`) and §1 forbids a `<Select>` at the top of a form whose
 * fields change with it.
 *
 * The types come from `GET /admin/ai-providers/adapters`, never from a
 * client-side list: the registry is the authority, and the hardcoded map this
 * codebase still carries for other surfaces omits one of the five.
 *
 * Plain state rather than `react-hook-form` + zod, as the surface it replaces
 * also did: the key step's field list is defined at runtime by the chosen
 * adapter's `admin_config_schema`, so a static resolver would have to restate a
 * list the server had just sent. Validation is still per field and per step —
 * `Next` checks only its own step — and renders under the field it belongs to.
 */
export function ConnectProviderDialog() {
  const [isOpen, setIsOpen] = useState(false)
  const [step, setStep] = useState<Step>(1)
  const [type, setType] = useState<AICredentialType | null>(null)
  const [kind, setKind] = useState<AIProviderKind>("fixed_key")
  const [state, setState] = useState<CreateState>(EMPTY_STATE)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [conflict, setConflict] = useState<AutoProvisionConflict | null>(null)
  const fieldId = useId()

  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const {
    adapters,
    adapterFor,
    supportsMinting,
    typeLabel,
    isPending: adaptersPending,
    isError: adaptersError,
  } = useProviderAdapters(isOpen)

  const adapter = type ? adapterFor(type) : undefined
  const isMinted = kind === "minted"
  // **One list, used by the renderer, the validator and the payload.**
  //
  // `admin_config_schema` is keyed on the *type*, not on the kind, and OpenAI —
  // the only type that can mint — declares `project_id` as required. Deriving
  // the three uses separately let a `fixed_key` OpenAI provider be validated
  // against a field it never renders: Next and Save then did nothing, forever,
  // with no message anywhere on screen. The backend permits that shape
  // (`_validate_shape` requires `project_id` only under `kind == MINTED`), so
  // the surface must too.
  const configFields = useMemo(
    () => (isMinted ? adminConfigFields(adapter) : []),
    [adapter, isMinted],
  )
  const vendor = type ? typeLabel(type) : ""

  const reset = () => {
    setStep(1)
    setType(null)
    setKind("fixed_key")
    setState(EMPTY_STATE)
    setErrors({})
    setConflict(null)
    autoNameRef.current = ""
  }

  const patch = (next: Partial<CreateState>) =>
    setState((current) => ({ ...current, ...next }))

  // Choosing a type that cannot mint retracts a `minted` choice made against a
  // previous one, rather than leaving a doomed selection for the server to
  // refuse. Gated on `supports_minting` — the permanent capability — and never
  // on the retired `can_mint_now`, whose second term is "a provider of this
  // type already exists" and which is therefore false for the first OpenAI
  // provider an admin ever connects: the feature's headline scenario.
  useEffect(() => {
    if (type && isMinted && !supportsMinting(type)) setKind("fixed_key")
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [type, isMinted, adapters.length])

  // The name follows the type until the admin types their own.
  //
  // The ref is what makes "until": without it the first suggestion makes `name`
  // non-empty forever, so switching OpenAI → Anthropic afterwards ships a
  // provider still called "OpenAI keys".
  const autoNameRef = useRef("")
  useEffect(() => {
    if (!type) return
    const suggested = `${typeLabel(type)} keys`
    setState((current) =>
      current.name === "" || current.name === autoNameRef.current
        ? { ...current, name: suggested }
        : current,
    )
    autoNameRef.current = suggested
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [type, adapters.length])

  // The model pickers on step 3 probe with the key typed on step 2. A minted
  // provider's secret is an administration key, not a model key, so there is
  // nothing here to list models with.
  const canProbe =
    !isMinted &&
    !!type &&
    state.secret.trim() !== "" &&
    (!adapter?.requires_base_url || state.base_url.trim() !== "")
  const probeModels = () => {
    // `canProbe` already requires a type, and `probeDisabled` is derived from
    // it — but a cast would make this the one place a future caller could send
    // `null` as a vendor and get a confusing 422 back instead of a stack.
    if (!type) return Promise.reject(new Error("Choose a type first."))
    return AdminLlmProvidersService.testManagedAiCredentialConnection({
      requestBody: {
        type,
        api_key: state.secret.trim(),
        base_url: state.base_url.trim() || undefined,
      },
    })
  }

  const createMutation = useMutation({
    mutationFn: (body: AIProviderCreate) =>
      AdminAiProvidersService.createAiProvider({ requestBody: body }),
    onSuccess: () => {
      showSuccessToast("Provider connected. Press Verify to check it.")
      setIsOpen(false)
      reset()
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

  const validateStep = (which: Step): boolean => {
    const next: Record<string, string> = {}
    if (which === 1 && !type) next.type = "Choose a type"
    if (which === 2) {
      if (state.name.trim() === "") next.name = "Name is required"
      if (state.secret.trim() === "") {
        next.secret = isMinted
          ? "An administration key is required"
          : "An API key is required"
      }
      if (adapter?.requires_base_url && state.base_url.trim() === "") {
        next.base_url = "Base URL is required for this type"
      }
      if (adapter?.requires_model && state.model.trim() === "") {
        next.model = "Model is required for this type"
      }
      for (const field of configFields) {
        if (field.required && (state.config[field.name] ?? "").trim() === "") {
          next[`config.${field.name}`] = `${field.label} is required`
        }
      }
    }
    setErrors(next)
    return Object.keys(next).length === 0
  }

  const goNext = () => {
    if (!validateStep(step)) return
    setStep((current) => (current === 1 ? 2 : 3))
  }

  const submit = () => {
    if (!type) return
    if (!validateStep(2)) {
      setStep(2)
      return
    }
    // Built from the adapter's own field names, not a hardcoded list. Phase 4
    // made a misspelled config key a 422 rather than a silent drop, so a field
    // this renderer gets wrong now fails loudly instead of landing on the two
    // fields that decide which project keys are minted into.
    const config: Record<string, string | null> = {}
    for (const field of configFields) {
      config[field.name] = (state.config[field.name] ?? "").trim() || null
    }
    const availableModels = parseAvailableModels(state.available_models)
    const body: AIProviderCreate = {
      name: state.name.trim(),
      kind,
      type,
      secret: state.secret.trim(),
      // The double cast is deliberate and the specification requires it: the
      // payload's keys come from the adapter's `admin_config_schema`, and
      // naming `organization_id` / `project_id` in the client is exactly what
      // that rule forbids. `AIProviderConfigInput` is a closed two-field shape,
      // so a future adapter field would land as a 422 naming the key rather
      // than being dropped silently — which is the behaviour Phase 4 chose.
      config: isMinted
        ? (config as unknown as AIProviderConfigInput)
        : undefined,
      base_url: adapter?.requires_base_url
        ? state.base_url.trim() || undefined
        : undefined,
      model: adapter?.requires_model ? state.model.trim() || undefined : undefined,
      auto_provision_roles: state.auto_provision_roles,
      set_as_default: state.set_as_default,
      set_user_sdk_defaults: state.set_user_sdk_defaults,
      sdk_default_modes: state.sdk_default_modes,
      default_model: stripProviderPrefix(state.default_model) || undefined,
      // Omitted when empty so an unset curation stays NULL (offer everything).
      available_models: availableModels.length ? availableModels : undefined,
      // Nothing to clear on create; blank means "not set".
      model_override_conversation:
        stripProviderPrefix(state.model_override_conversation) || undefined,
      model_override_building:
        stripProviderPrefix(state.model_override_building) || undefined,
    }
    createMutation.mutate(body)
  }

  const isSaving = createMutation.isPending
  const adaptersReady = !adaptersPending && !adaptersError

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        setIsOpen(open)
        if (!open) reset()
      }}
    >
      <DialogTrigger asChild>
        <Button>
          <Plus className="mr-2 h-4 w-4" />
          Connect provider
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg max-h-[90vh] overflow-y-auto">
        {/* One <form> with hidden panes, as the P6 reference does: it keeps
            typed values across steps and gives Enter a per-step meaning. */}
        <form
          onSubmit={(event) => {
            event.preventDefault()
            if (step === 3) submit()
            else goNext()
          }}
        >
          <DialogHeader>
            <p className="text-xs text-muted-foreground">
              <span
                className={step === 1 ? "font-medium text-foreground" : undefined}
              >
                1 Source
              </span>
              {" · "}
              <span
                className={step === 2 ? "font-medium text-foreground" : undefined}
              >
                2 Key
              </span>
              {" · "}
              <span
                className={step === 3 ? "font-medium text-foreground" : undefined}
              >
                3 Who gets one
              </span>
            </p>
            <DialogTitle>
              {step === 1
                ? "Connect provider"
                : step === 2
                  ? "Key"
                  : "Who gets one"}
            </DialogTitle>
            <DialogDescription>
              {step === 1
                ? "A provider holds a key and the rule for handing it out. Start with the vendor it talks to."
                : step === 2
                  ? `What Cinna needs to reach ${vendor || "the vendor"}.`
                  : "Which new accounts receive this key, and how it is wired for them."}
            </DialogDescription>
          </DialogHeader>

          {/* ── Step 1 — Source ─────────────────────────────────────── */}
          <fieldset
            className="grid min-w-0 gap-4 py-4"
            hidden={step !== 1}
            disabled={isSaving}
          >
            <div className="space-y-2">
              <Label className="text-sm font-medium">
                Type <span className="text-destructive">*</span>
              </Label>
              {adaptersPending ? (
                <div className="flex flex-wrap gap-2">
                  {[0, 1, 2].map((index) => (
                    <Skeleton key={index} className="h-8 w-28 rounded-full" />
                  ))}
                </div>
              ) : adaptersError ? (
                <p className="text-sm text-destructive">
                  The type list could not be loaded, so a provider cannot be
                  connected. Refresh and try again.
                </p>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {adapters.map((entry) => (
                    <button
                      key={entry.type}
                      type="button"
                      onClick={() => setType(entry.type)}
                      aria-pressed={type === entry.type}
                      className={cn(
                        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm font-medium transition-colors",
                        type === entry.type
                          ? "border-primary bg-primary text-primary-foreground"
                          : "hover:bg-muted",
                      )}
                    >
                      {entry.label}
                    </button>
                  ))}
                </div>
              )}
              <p className="text-xs text-muted-foreground">
                The vendor this provider talks to. It cannot be changed once the
                provider is connected.
              </p>
              {errors.type && (
                <p className="text-xs text-destructive">{errors.type}</p>
              )}
            </div>

            <div className="space-y-2">
              <Label className="text-sm font-medium">
                Kind <span className="text-destructive">*</span>
              </Label>
              <div className="grid gap-2">
                {AI_PROVIDER_KINDS.map((entry) => {
                  const unavailable =
                    entry.value === "minted" &&
                    (!type || !supportsMinting(type))
                  return (
                    <div key={entry.value} className="space-y-1">
                      <button
                        type="button"
                        role="radio"
                        aria-checked={kind === entry.value}
                        disabled={unavailable}
                        onClick={() => setKind(entry.value)}
                        className={cn(
                          "w-full rounded-md border p-3 text-left transition-colors",
                          kind === entry.value
                            ? "border-primary bg-primary/5"
                            : "hover:bg-muted/50",
                          unavailable && "opacity-60",
                        )}
                      >
                        <span className="text-sm font-medium">
                          {entry.label}
                        </span>
                        <p className="text-xs text-muted-foreground">
                          {entry.blurb}
                        </p>
                      </button>
                      {/* A disabled tile never sits here without the reason
                          beside it, in text — that is §4's anti-pattern, and
                          both of the two ways this tile can be unavailable get
                          a sentence. The vendor case is composed from the
                          adapter's own label and `supports_minting`, so a sixth
                          adapter needs no copy change here. */}
                      {unavailable && adaptersReady && (
                        <p className="px-3 text-xs text-muted-foreground">
                          {type ? (
                            <>
                              {vendor} cannot create keys through its API, so a{" "}
                              {vendor} provider shares one pasted key. That is a
                              normal setup, not a limitation of this server.
                            </>
                          ) : (
                            <>
                              Choose a type first — only some vendors can create
                              keys through their API.
                            </>
                          )}
                        </p>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          </fieldset>

          {/* ── Step 2 — Key ────────────────────────────────────────── */}
          <fieldset
            className="grid min-w-0 gap-4 py-4"
            hidden={step !== 2}
            disabled={isSaving}
          >
            <div className="space-y-2">
              <Label htmlFor={`${fieldId}-name`}>
                Name <span className="text-destructive">*</span>
              </Label>
              <Input
                id={`${fieldId}-name`}
                value={state.name}
                onChange={(event) => patch({ name: event.target.value })}
              />
              <p className="text-xs text-muted-foreground">
                What this key source is called in the list.
              </p>
              {errors.name && (
                <p className="text-xs text-destructive">{errors.name}</p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor={`${fieldId}-secret`}>
                {isMinted ? "Administration key" : "API key"}{" "}
                <span className="text-destructive">*</span>
              </Label>
              <Input
                id={`${fieldId}-secret`}
                type="password"
                autoComplete="off"
                placeholder={isMinted ? "sk-admin-…" : "sk-…"}
                value={state.secret}
                onChange={(event) => patch({ secret: event.target.value })}
              />
              <p className="text-xs text-muted-foreground">
                {isMinted
                  ? "The organisation key Cinna uses to create each person's key. It is never handed to a member and never shown again after saving."
                  : "The model API key every member will hold a copy of."}
              </p>
              {errors.secret && (
                <p className="text-xs text-destructive">{errors.secret}</p>
              )}
            </div>

            {adapter?.requires_base_url && (
              <div className="space-y-2">
                <Label htmlFor={`${fieldId}-base-url`}>
                  Base URL <span className="text-destructive">*</span>
                </Label>
                <Input
                  id={`${fieldId}-base-url`}
                  placeholder="https://api.example.com/v1"
                  value={state.base_url}
                  onChange={(event) => patch({ base_url: event.target.value })}
                />
                {errors.base_url && (
                  <p className="text-xs text-destructive">{errors.base_url}</p>
                )}
              </div>
            )}

            {adapter?.requires_model && (
              <div className="space-y-2">
                <Label htmlFor={`${fieldId}-model`}>
                  Model <span className="text-destructive">*</span>
                </Label>
                <Input
                  id={`${fieldId}-model`}
                  placeholder="meta-llama/Llama-3-70b"
                  value={state.model}
                  onChange={(event) => patch({ model: event.target.value })}
                />
                {errors.model && (
                  <p className="text-xs text-destructive">{errors.model}</p>
                )}
              </div>
            )}

            {isMinted && (
              <>
                {configFields.map((field) => (
                  <div key={field.name} className="space-y-2">
                    <Label htmlFor={`${fieldId}-${field.name}`}>
                      {field.label}
                      {field.required && (
                        <span className="text-destructive"> *</span>
                      )}
                    </Label>
                    <Input
                      id={`${fieldId}-${field.name}`}
                      inputMode={
                        field.type === "integer" ? "numeric" : undefined
                      }
                      value={state.config[field.name] ?? ""}
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
                {/* §9's rule: read, never written. The other half — set a
                    limit, in the vendor's console — is already carried by the
                    adapter's own `help` for the project field above, so this
                    says only the part that is Cinna's to state rather than
                    repeating the sentence 40 words later. There is no
                    spend-limit input anywhere: the number lives in the vendor's
                    console and a copy here would be a second one. */}
                <p className="text-xs text-muted-foreground">
                  Cinna only ever reads the project's monthly spend limit —
                  it never sets one, and never stores a copy of it.
                </p>
              </>
            )}
          </fieldset>

          {/* ── Step 3 — Who gets one ───────────────────────────────── */}
          <fieldset
            className="grid min-w-0 gap-4 py-4"
            hidden={step !== 3}
            disabled={isSaving}
          >
            <ProvisioningPolicyFields
              value={state}
              onChange={patch}
              idPrefix={`${fieldId}-policy`}
              type={type ?? undefined}
              conflict={conflict}
              probeModels={probeModels}
              probeDisabled={!canProbe || isSaving}
              disabled={isSaving}
            />
          </fieldset>

          <DialogFooter>
            {step === 1 ? (
              <Button
                type="button"
                variant="outline"
                onClick={() => setIsOpen(false)}
              >
                Cancel
              </Button>
            ) : (
              <Button
                type="button"
                variant="outline"
                disabled={isSaving}
                onClick={() => setStep(step === 3 ? 2 : 1)}
              >
                Back
              </Button>
            )}
            {step === 3 ? (
              <LoadingButton type="submit" loading={isSaving}>
                Connect provider
              </LoadingButton>
            ) : (
              <Button
                type="button"
                disabled={!adaptersReady || isSaving}
                onClick={goNext}
              >
                Next
              </Button>
            )}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
