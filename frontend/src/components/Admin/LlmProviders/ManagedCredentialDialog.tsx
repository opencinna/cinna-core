import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { CheckCircle2, ExternalLink, Loader2, Plus } from "lucide-react"
import { useEffect, useId, useRef, useState } from "react"
import { useForm, type UseFormReturn } from "react-hook-form"
import { z } from "zod"

import {
  type AICredentialTestResult,
  type AICredentialType,
  type ManagedAICredentialCreate,
  type ManagedAICredentialPublic,
  type ManagedAICredentialReconcileResult,
  type ManagedAICredentialUpdate,
  AdminLlmProvidersService,
} from "@/client"
import type { ApiError } from "@/client/core/ApiError"
import { ListModelsButton } from "@/components/Common/ListModelsButton"
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { USER_ROLE_OPTIONS } from "@/utils/userRoles"
import {
  type AutoProvisionConflict,
  describeAutoProvisionConflict,
  getProviderTypeLabel,
  MANAGED_CREDENTIALS_QUERY_PREFIX,
  modelOverrideField,
  parseAutoProvisionConflict,
  PROVIDER_TYPE_OPTIONS,
  SDK_MODE_OPTIONS,
  SDK_MODE_VALUES,
} from "./providerTypes"

// Default credential name suggested for a freshly-provisioned record,
// derived from the selected provider (e.g. "Anthropic Key").
function defaultCredentialName(type: AICredentialType): string {
  return `${getProviderTypeLabel(type)} Key`
}

// Human-readable copy for the test-connection skip reasons. A skip means the
// connection is valid but model listing isn't applicable for this credential.
const TEST_SKIP_MESSAGES: Record<string, string> = {
  oauth_token_unsupported:
    "Connection valid — model listing isn't supported for OAuth tokens.",
  no_list_endpoint: "Connection valid — this provider doesn't expose a model list.",
  no_base_url: "Enter a Base URL to list available models.",
  unsupported_type: "Connection valid — model listing not supported.",
}

function describeTestResult(result: AICredentialTestResult): string {
  if (result.success) {
    if (result.skip_reason && TEST_SKIP_MESSAGES[result.skip_reason]) {
      return TEST_SKIP_MESSAGES[result.skip_reason]
    }
    return `Connection successful — ${result.model_count} model${
      result.model_count === 1 ? "" : "s"
    } available.`
  }
  if (result.error === "invalid_key") {
    return "Connection failed — the provider rejected this key."
  }
  return "Connection failed."
}

// Validation mirrors the field rules documented in ai_credentials.md:
//  - openai_compatible requires both base_url and model
//  - google may set an optional base_url
//  - all others use neither
// Base object shape — `FormData` is inferred from this so the form type stays
// stable across modes. The per-field rules below are applied via `superRefine`.
const baseFormSchema = z.object({
  name: z.string().min(1, "Name is required"),
  type: z.enum(["anthropic", "openai", "openai_compatible", "google"]),
  api_key: z.string(),
  base_url: z.string().optional(),
  model: z.string().optional(),
  // Admin-curated default model (single concrete id). Optional.
  default_model: z.string().optional(),
  // Admin-curated available-models list, edited as a comma/newline-separated
  // string; parsed into a deduped list on submit.
  available_models: z.string().optional(),
  set_as_default: z.boolean(),
  set_user_sdk_defaults: z.boolean(),
  // Which of the member's two default slots this record claims. Only read when
  // `set_user_sdk_defaults` is on, but still seeded from (and shown as) the
  // stored value, so turning the switch back on reveals what is actually
  // saved. Like every other edit field it travels only when the admin changed
  // it — see the diff in the submit handler.
  sdk_default_modes: z.array(z.string()),
  // Roles whose newly created accounts receive this credential.
  auto_provision_roles: z.array(z.string()),
  // Per-mode model pinned on the member's profile alongside the default
  // credential. Blank = the record has no opinion.
  model_override_conversation: z.string().optional(),
  model_override_building: z.string().optional(),
})

type FormData = z.infer<typeof baseFormSchema>

// Strip any leading "provider/" prefix for nicer display (the backend
// re-normalizes regardless).
function stripProviderPrefix(value: string): string {
  const trimmed = value.trim()
  const idx = trimmed.indexOf("/")
  return idx >= 0 ? trimmed.slice(idx + 1) : trimmed
}

// Official "available models" documentation page per provider. openai_compatible
// has no canonical page (the model list depends on the configured endpoint), so
// it's intentionally absent and the link is omitted for that type.
const PROVIDER_MODELS_DOC_URL: Partial<Record<AICredentialType, string>> = {
  anthropic: "https://platform.claude.com/docs/en/about-claude/models/overview",
  google: "https://ai.google.dev/gemini-api/docs/models",
  openai: "https://developers.openai.com/api/docs/models",
}

// Canonical Gemini alias used as the Google default — a stable pointer to the
// latest Flash model that may not appear verbatim in the discovered list.
const GOOGLE_DEFAULT_MODEL = "gemini-flash-latest"

// Pick the best default model id for the given provider from a (prefix-stripped)
// model list. Provider-specific:
//  - google: always the canonical Flash alias (independent of the list).
//  - anthropic: the highest-version Sonnet, tie-broken by trailing snapshot.
//  - openai / openai_compatible: the first list entry.
function pickDefaultModel(type: AICredentialType, models: string[]): string {
  if (type === "google") return GOOGLE_DEFAULT_MODEL
  if (type === "anthropic") {
    const sonnet = pickHighestSonnet(models)
    if (sonnet) return sonnet
  }
  return models[0] ?? ""
}

// Among model ids containing "sonnet", choose the highest version by comparing
// numeric version tokens (e.g. the "4-6" in "claude-sonnet-4-6"), tie-broken by
// any trailing dated snapshot (e.g. "...-20250115"). Returns undefined if none.
function pickHighestSonnet(models: string[]): string | undefined {
  const sonnets = models.filter((m) => m.toLowerCase().includes("sonnet"))
  if (sonnets.length === 0) return undefined

  // Split an id into its version tokens and a trailing dated snapshot, kept in
  // separate fields so they are never compared against each other positionally.
  // The version is the run of numeric tokens (e.g. [4, 6] in "claude-sonnet-4-6"),
  // and the snapshot is a trailing 6+ digit run (e.g. 20250115 in "...-20250115").
  const parseKey = (id: string): { version: number[]; snapshot: number } => {
    const snapshotMatch = id.match(/(\d{6,})\s*$/)
    const snapshot = snapshotMatch ? Number(snapshotMatch[1]) : 0
    // Numeric tokens excluding a trailing snapshot make up the version.
    const body = snapshotMatch ? id.slice(0, snapshotMatch.index) : id
    const version = (body.match(/\d+/g) ?? []).map(Number)
    // Ids with no numeric version compare as version [0].
    return { version: version.length > 0 ? version : [0], snapshot }
  }

  return sonnets.reduce((best, candidate) => {
    const a = parseKey(candidate)
    const b = parseKey(best)

    // Phase 1: compare version tokens element-by-element (missing slot = 0).
    // A longer version that is otherwise an equal prefix wins (e.g. 4-5 > 4).
    const len = Math.max(a.version.length, b.version.length)
    for (let i = 0; i < len; i++) {
      const av = a.version[i] ?? 0
      const bv = b.version[i] ?? 0
      if (av !== bv) return av > bv ? candidate : best
    }

    // Phase 2: version arrays are fully equal — break the tie by snapshot.
    if (a.snapshot !== b.snapshot) return a.snapshot > b.snapshot ? candidate : best

    // Exact full tie: keep the first occurrence (stable).
    return best
  })
}

// Parse the free-form available-models textarea into a deduped, prefix-stripped
// list. Accepts commas and newlines as separators.
function parseAvailableModels(raw: string | undefined): string[] {
  if (!raw) return []
  const seen = new Set<string>()
  const out: string[] = []
  for (const part of raw.split(/[\n,]/)) {
    const entry = stripProviderPrefix(part)
    if (entry && !seen.has(entry)) {
      seen.add(entry)
      out.push(entry)
    }
  }
  return out
}

// In edit mode the API key is optional (blank = keep the stored key); the
// required check is applied conditionally in `superRefine` keyed off `mode`.
function buildFormSchema(mode: "create" | "edit") {
  return baseFormSchema.superRefine((data, ctx) => {
    if (mode === "create" && data.api_key.trim() === "") {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["api_key"],
        message: "API key is required",
      })
    }
    if (data.type === "openai_compatible") {
      if (!data.base_url || data.base_url.trim() === "") {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["base_url"],
          message: "Base URL is required for OpenAI Compatible providers",
        })
      }
      if (!data.model || data.model.trim() === "") {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["model"],
          message: "Model is required for OpenAI Compatible providers",
        })
      }
    }
  })
}

// Mirrors the backend's `_default_sdk_modes()` so a record created here and one
// created by an API caller that omits the field start out the same — which is
// every mode there is.
// Copied, not aliased: this array is a form default and `SDK_MODE_VALUES` is
// exported to several other surfaces.
const DEFAULT_SDK_MODES = [...SDK_MODE_VALUES]

const CREATE_DEFAULTS: FormData = {
  name: defaultCredentialName("anthropic"),
  type: "anthropic",
  api_key: "",
  base_url: "",
  model: "",
  default_model: "",
  available_models: "",
  set_as_default: false,
  set_user_sdk_defaults: false,
  sdk_default_modes: DEFAULT_SDK_MODES,
  auto_provision_roles: [],
  model_override_conversation: "",
  model_override_building: "",
}

// Build the initial picker selection from an edit record's members.
function membersToTargets(
  record: ManagedAICredentialPublic | undefined,
): UserAllowlistSelectedItem[] {
  if (!record) return []
  return (record.members ?? []).map((m) => ({
    id: m.user_id,
    userId: m.user_id,
    fallbackLabel: m.full_name ? `${m.full_name} <${m.email}>` : m.email,
  }))
}

// Build form defaults from an edit record (api_key always blank — the stored
// key is never returned).
function recordToFormData(record: ManagedAICredentialPublic): FormData {
  return {
    name: record.name,
    // Managed records are always one of the four UI-selectable providers
    // (minimax is not exposed); narrow to the form's enum.
    type: record.type as FormData["type"],
    api_key: "",
    base_url: record.base_url ?? "",
    model: record.model ?? "",
    default_model: record.default_model ?? "",
    available_models: (record.available_models ?? []).join("\n"),
    set_as_default: record.set_as_default ?? false,
    set_user_sdk_defaults: record.set_user_sdk_defaults ?? false,
    sdk_default_modes: record.sdk_default_modes ?? DEFAULT_SDK_MODES,
    auto_provision_roles: record.auto_provision_roles ?? [],
    model_override_conversation: record.model_override_conversation ?? "",
    model_override_building: record.model_override_building ?? "",
  }
}

interface ModelOverrideFieldProps {
  form: UseFormReturn<FormData>
  name: "model_override_conversation" | "model_override_building"
  modeLabel: string
  /** The value already stored on the record; `null` in create mode. */
  storedValue: string | null
  /** Fetch this credential's live model list for the picker. */
  probeModels: () => Promise<AICredentialTestResult>
  /** True while the form is saving or there is no key to probe with. */
  probeDisabled: boolean
}

/**
 * One mode's model override, with the shared model picker beside it.
 *
 * Its own component because the two modes differ only by field name, and the
 * field name has to be a literal for `react-hook-form` to type the value.
 */
function ModelOverrideField({
  form,
  name,
  modeLabel,
  storedValue,
  probeModels,
  probeDisabled,
}: ModelOverrideFieldProps) {
  // Emptying the box is a real edit: the submit handler sends `""`, the
  // backend stores NULL and unpins every member still carrying the value
  // being dropped. Say what that costs while the box is empty, since the
  // consequence lands on other people's profiles rather than on this screen.
  const clearing = !!storedValue && (form.watch(name) ?? "").trim() === ""

  return (
    <FormField
      control={form.control}
      name={name}
      render={({ field }) => (
        <FormItem>
          <FormLabel>{modeLabel} model override</FormLabel>
          <div className="flex items-start gap-2">
            <FormControl>
              <Input
                placeholder="Leave blank to use the credential's default model"
                {...field}
                value={field.value ?? ""}
              />
            </FormControl>
            <ListModelsButton
              credentialId={null}
              credentialType={null}
              probeModels={probeModels}
              disabled={probeDisabled}
              // The picker hands back the provider's id verbatim, and the
              // backend stores `_normalize_default_model` of it. Stripping here
              // rather than only on submit keeps the box showing the value that
              // will actually be saved.
              onSelect={(modelId) =>
                form.setValue(name, stripProviderPrefix(modelId), {
                  shouldDirty: true,
                })
              }
            />
          </div>
          <FormDescription>
            Pinned as each member's {modeLabel.toLowerCase()} model when this
            credential becomes their default for that mode. Leave blank for no
            opinion — members fall back to the credential's own default model.
          </FormDescription>
          {clearing && (
            <p className="text-xs text-amber-600 dark:text-amber-500">
              Saving unpins "{storedValue}" from members who still have it.
              Anyone who picked their own model keeps it.
            </p>
          )}
          <FormMessage />
        </FormItem>
      )}
    />
  )
}

interface ManagedCredentialDialogProps {
  mode: "create" | "edit"
  // The record being edited (required for mode === "edit").
  record?: ManagedAICredentialPublic
  // Controlled open state — used by the actions menu in edit mode. In create
  // mode the dialog supplies its own header trigger button and manages state.
  open?: boolean
  onOpenChange?: (open: boolean) => void
}

export function ManagedCredentialDialog({
  mode,
  record,
  open: controlledOpen,
  onOpenChange,
}: ManagedCredentialDialogProps) {
  // Create mode manages its own open state (triggered by the header button);
  // edit mode is fully controlled by the parent actions menu.
  const [internalOpen, setInternalOpen] = useState(false)
  const isOpen = mode === "edit" ? (controlledOpen ?? false) : internalOpen
  const setIsOpen = (open: boolean) => {
    if (mode === "edit") onOpenChange?.(open)
    else setInternalOpen(open)
  }

  // Every row of the providers table mounts one of these dialogs, so the
  // checkbox ids below have to be unique per instance even though Radix only
  // keeps the open one's content in the DOM.
  const fieldId = useId()

  const [targets, setTargets] = useState<UserAllowlistSelectedItem[]>(() =>
    membersToTargets(record),
  )
  // Whether the admin actually touched the membership picker.
  //
  // `target_user_ids` is an *absolute* desired set: whatever is not in it gets
  // removed. `targets` is a snapshot taken when the dialog opened, and the
  // record behind it keeps moving — a signup auto-provisions, another admin
  // runs Apply to existing users, a window-focus refetch lands. Submitting the
  // snapshot unconditionally would delete every member acquired since the
  // dialog opened, which is precisely the thing auto-provisioning does all day.
  // `undefined` means "leave membership alone" to the backend, so the picker is
  // only sent when it was actually used.
  const [membershipDirty, setMembershipDirty] = useState(false)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const form = useForm<FormData>({
    resolver: zodResolver(buildFormSchema(mode)),
    mode: "onBlur",
    defaultValues: mode === "edit" && record ? recordToFormData(record) : CREATE_DEFAULTS,
  })

  // The values the form was last seeded with — i.e. what the record looked
  // like when this dialog opened. Every PATCH field is diffed against this,
  // for the same reason `membershipDirty` exists: the payload is *absolute*,
  // the snapshot is stale the moment it is taken (a signup auto-provisions,
  // another admin edits the record, a window-focus refetch lands), and
  // resubmitting an untouched field asserts a value the admin never chose.
  // For the auto-provision fields that is not merely redundant — it is how a
  // rename comes back as a 409 for a slot conflict the admin did not
  // introduce. Sending only what changed is the fix; the backend's
  // transition-scoped validator is the backstop under it.
  // Recomputed on every render and discarded after the first — cheap enough
  // for a dialog, and it keeps the ref non-nullable so every read below is a
  // real `FormData`. The spread matters: the ref outlives the render, and the
  // module-level defaults must not become per-instance state.
  const openedWithRef = useRef<FormData>(
    mode === "edit" && record ? recordToFormData(record) : { ...CREATE_DEFAULTS },
  )
  const seedForm = (values: FormData) => {
    openedWithRef.current = values
    form.reset(values)
  }

  const selectedType = form.watch("type") as AICredentialType
  const selectedApiKey = form.watch("api_key")
  const selectedBaseUrl = form.watch("base_url")
  const showBaseUrl = selectedType === "openai_compatible" || selectedType === "google"
  const showModel = selectedType === "openai_compatible"
  const setUserSdkDefaults = form.watch("set_user_sdk_defaults")
  const sdkModes = form.watch("sdk_default_modes")
  const autoProvisionRoles = form.watch("auto_provision_roles")

  // The 409 raised when another managed credential already owns a
  // (role, mode) default slot this one is claiming. Rendered next to the
  // controls that caused it rather than as a toast: the fix is to untick one
  // of them, and a toast is gone by the time the admin looks for it.
  const [conflict, setConflict] = useState<AutoProvisionConflict | null>(null)
  useEffect(() => {
    setConflict(null)
    // Only the three inputs the rule reads. Keyed by value, not by array
    // identity, so an unrelated re-render does not clear a live error.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setUserSdkDefaults, sdkModes.join(","), autoProvisionRoles.join(",")])

  const toggleInArray = (
    field: "sdk_default_modes" | "auto_provision_roles",
    value: string,
    checked: boolean,
  ) => {
    const current = form.getValues(field)
    const next = checked
      ? current.includes(value)
        ? current
        : [...current, value]
      : current.filter((entry) => entry !== value)
    form.setValue(field, next, { shouldDirty: true })
  }

  // Test Connection result (inline alert). Cleared whenever the inputs that
  // feed the probe change, so a stale result never lingers.
  const [testResult, setTestResult] = useState<AICredentialTestResult | null>(null)
  useEffect(() => {
    setTestResult(null)
  }, [selectedType, selectedApiKey, selectedBaseUrl])

  // Tracks the last auto-suggested name so we only overwrite it while the user
  // hasn't typed their own name. Only active in create mode.
  const autoNameRef = useRef(CREATE_DEFAULTS.name)

  // Keep the suggested name in sync with the selected provider until the user
  // types their own name. Disabled in edit mode (provider type is immutable).
  useEffect(() => {
    if (mode === "edit") return
    const currentName = form.getValues("name")
    if (currentName === "" || currentName === autoNameRef.current) {
      const suggested = defaultCredentialName(selectedType)
      autoNameRef.current = suggested
      form.setValue("name", suggested)
    }
  }, [selectedType, form, mode])

  // Re-seed the form + picker whenever the edit dialog opens for a record, so a
  // stale local edit from a previously-closed dialog never leaks in.
  useEffect(() => {
    if (mode === "edit" && isOpen && record) {
      seedForm(recordToFormData(record))
      setTargets(membersToTargets(record))
      setMembershipDirty(false)
      setTestResult(null)
      setConflict(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, record?.id])

  const resetDialog = () => {
    if (mode === "edit" && record) {
      seedForm(recordToFormData(record))
      setTargets(membersToTargets(record))
    } else {
      // A copy: the ref holds this object for the life of the dialog, and the
      // module-level defaults must not become per-instance state.
      seedForm({ ...CREATE_DEFAULTS })
      autoNameRef.current = CREATE_DEFAULTS.name
      setTargets([])
    }
    setMembershipDirty(false)
    setTestResult(null)
    setConflict(null)
  }

  // Test Connection mutation — validates the entered key without persisting
  // anything. In edit-with-blank-key mode we pass the record id so the backend
  // probes via the stored parent key instead.
  const testMutation = useMutation({
    mutationFn: () =>
      AdminLlmProvidersService.testManagedAiCredentialConnection({
        managedCredentialId:
          mode === "edit" && record && !(selectedApiKey && selectedApiKey.trim() !== "")
            ? record.id
            : undefined,
        requestBody: {
          type: selectedType,
          api_key: selectedApiKey?.trim() ? selectedApiKey : undefined,
          base_url: showBaseUrl ? selectedBaseUrl || undefined : undefined,
        },
      }),
    onSuccess: (result) => setTestResult(result),
    onError: (err: Error) =>
      setTestResult({
        success: false,
        models: [],
        model_count: 0,
        error: err.message || "Connection failed",
      }),
  })

  // The same probe, minus the shared inline alert.
  //
  // The model picker needs exactly what Test Connection fetches, but it owns
  // its own pending / error / empty rendering. Routing it through
  // `testMutation` would make opening a picker repaint the red-or-green
  // Test Connection banner at the foot of the dialog — a result the admin
  // never asked for, reporting on a probe they did not run — and would let the
  // picker's own Retry re-enter an already-pending mutation.
  const probeModelsForOverride = () =>
    AdminLlmProvidersService.testManagedAiCredentialConnection({
      managedCredentialId:
        mode === "edit" && record && !(selectedApiKey && selectedApiKey.trim() !== "")
          ? record.id
          : undefined,
      requestBody: {
        type: selectedType,
        api_key: selectedApiKey?.trim() ? selectedApiKey : undefined,
        base_url: showBaseUrl ? selectedBaseUrl || undefined : undefined,
      },
    })

  // Test Connection is allowed once there's a key to probe (entered key, or —
  // in edit mode — the stored parent key), plus a base URL for
  // OpenAI-compatible providers.
  const canTest = (): boolean => {
    if (testMutation.isPending) return false
    const hasEnteredKey = !!selectedApiKey && selectedApiKey.trim() !== ""
    const hasStoredKey = mode === "edit" && !!record?.has_api_key
    if (!hasEnteredKey && !hasStoredKey) return false
    if (selectedType === "openai_compatible")
      return !!selectedBaseUrl && selectedBaseUrl.trim() !== ""
    return true
  }

  // "Fill top 10 models": reuse a fresh successful test result if one exists for
  // the current inputs, otherwise run the Test Connection first and continue
  // once it resolves. On success, populate the Available models field with the
  // top 10 discovered models (provider order, deduped, prefix-stripped) and set
  // a provider-appropriate default model. A failed/empty test fills nothing —
  // the inline test-result alert explains why.
  const fillTopModels = async () => {
    let result = testResult
    if (!(result?.success && (result.models?.length ?? 0) > 0)) {
      try {
        result = await testMutation.mutateAsync()
      } catch {
        // mutation onError already surfaces the failure via testResult; nothing
        // to fill.
        return
      }
    }
    if (!result?.success || (result.models?.length ?? 0) === 0) return

    const deduped: string[] = []
    const seen = new Set<string>()
    for (const raw of result.models ?? []) {
      const entry = stripProviderPrefix(raw)
      if (entry && !seen.has(entry)) {
        seen.add(entry)
        deduped.push(entry)
      }
    }
    const top = deduped.slice(0, 10)
    if (top.length === 0) return

    form.setValue("available_models", top.join("\n"), { shouldDirty: true })
    // The user explicitly invoked fill, so overwriting the default is expected.
    const nextDefault = pickDefaultModel(selectedType, deduped)
    if (nextDefault) {
      form.setValue("default_model", nextDefault, { shouldDirty: true })
    }
  }

  // Map a reconcile result into per-user warning toasts + a success summary.
  const surfaceReconcileResult = (
    result: ManagedAICredentialReconcileResult,
    pickedTargets: UserAllowlistSelectedItem[],
  ) => {
    const added = result.added ?? []
    const removed = result.removed ?? []
    const updatedCount = result.updated_count ?? 0
    const skipped = result.skipped ?? []
    const blocked = result.blocked ?? []

    // Resolve user ids back to the labels we have (members carry their own
    // email/name; picked targets carry the fallback label) for readable toasts.
    const labelById = new Map<string, string>()
    for (const m of result.record.members ?? []) {
      labelById.set(m.user_id, m.full_name ? `${m.full_name} <${m.email}>` : m.email)
    }
    for (const t of pickedTargets) {
      if (!labelById.has(t.userId)) {
        labelById.set(t.userId, t.fallbackLabel || t.userId)
      }
    }
    const labelFor = (userId: string) => labelById.get(userId) ?? userId

    // Per-user warnings: blocked (in use by a bundle) and skipped (unknown /
    // inactive). One toast per entry so the admin sees each by name.
    for (const b of blocked) {
      showErrorToast(`${labelFor(b.user_id)} not removed — in use by a published bundle.`)
    }
    for (const s of skipped) {
      showErrorToast(`${labelFor(s.user_id)} skipped (${s.reason}).`)
    }

    const summaryParts: string[] = []
    if (added.length) summaryParts.push(`+${added.length} added`)
    if (removed.length) summaryParts.push(`−${removed.length} removed`)
    if (updatedCount) summaryParts.push(`~${updatedCount} updated`)

    if (summaryParts.length) {
      showSuccessToast(
        `${mode === "create" ? "Credential provisioned" : "Credential updated"} (${summaryParts.join(", ")}).`,
      )
    } else if (blocked.length === 0 && skipped.length === 0) {
      showSuccessToast(
        mode === "create" ? "Credential provisioned." : "No changes to apply.",
      )
    }
  }

  // A (role, mode) collision is the one failure the admin can fix without
  // leaving the dialog, so it is kept on screen; everything else is a toast as
  // before.
  const handleSaveError = (error: unknown) => {
    const detected = parseAutoProvisionConflict(error)
    if (detected) {
      setConflict(detected)
      return
    }
    handleError.call(showErrorToast, error as ApiError)
  }

  const createMutation = useMutation({
    mutationFn: (body: ManagedAICredentialCreate) =>
      AdminLlmProvidersService.createManagedAiCredential({ requestBody: body }),
    onSuccess: (result) => {
      surfaceReconcileResult(result, targets)
      resetDialog()
      setIsOpen(false)
    },
    onError: handleSaveError,
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX })
    },
  })

  const updateMutation = useMutation({
    mutationFn: (body: ManagedAICredentialUpdate) =>
      AdminLlmProvidersService.updateManagedAiCredential({
        managedCredentialId: record!.id,
        requestBody: body,
      }),
    onSuccess: (result) => {
      surfaceReconcileResult(result, targets)
      setIsOpen(false)
    },
    onError: handleSaveError,
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX })
    },
  })

  const isPending = createMutation.isPending || updateMutation.isPending

  // Members present on the record but no longer in the picker — i.e. what this
  // save would delete.
  const removedMemberCount =
    mode === "edit"
      ? (record?.members ?? []).filter(
          (member) => !targets.some((t) => t.userId === member.user_id),
        ).length
      : 0

  const onSubmit = (data: FormData) => {
    // Create only. A brand-new record with neither members nor auto-provision
    // roles would do nothing at all, so it is refused; an auto-provision-only
    // record is the new legitimate case, starting empty and filling up as
    // accounts are created. On edit there is nothing to guard: membership is
    // only sent when the picker was touched, and unticking the last role on an
    // as-yet-empty record is a state the backend accepts.
    if (
      mode === "create" &&
      targets.length === 0 &&
      data.auto_provision_roles.length === 0
    ) {
      showErrorToast(
        "Select at least one target user, or a role to auto-provision for.",
      )
      return
    }

    const includesBaseUrl = data.type === "openai_compatible" || data.type === "google"
    const includesModel = data.type === "openai_compatible"
    const targetUserIds = targets.map((t) => t.userId)

    const defaultModel = stripProviderPrefix(data.default_model ?? "")
    const availableModels = parseAvailableModels(data.available_models)
    // `""` is the documented clear on `ManagedAICredentialUpdate`: it stores
    // NULL and unpins the members still carrying the value being dropped.
    // "Leave alone" is expressed by omitting the field entirely, which is what
    // the dirty check below does — so a blank box that the admin never touched
    // is never sent as a clear.
    const overrideConversation = stripProviderPrefix(
      data.model_override_conversation ?? "",
    )
    const overrideBuilding = stripProviderPrefix(
      data.model_override_building ?? "",
    )

    if (mode === "create") {
      const body: ManagedAICredentialCreate = {
        name: data.name.trim(),
        type: data.type,
        api_key: data.api_key,
        base_url: includesBaseUrl ? data.base_url?.trim() || undefined : undefined,
        model: includesModel ? data.model?.trim() || undefined : undefined,
        default_model: defaultModel || undefined,
        // Omit when empty so an unset curation stays NULL (offer all discovered).
        available_models: availableModels.length ? availableModels : undefined,
        target_user_ids: targetUserIds,
        set_as_default: data.set_as_default,
        set_user_sdk_defaults: data.set_user_sdk_defaults,
        sdk_default_modes: data.sdk_default_modes,
        auto_provision_roles: data.auto_provision_roles,
        // On create there is nothing to clear; blank means "not set".
        model_override_conversation: overrideConversation || undefined,
        model_override_building: overrideBuilding || undefined,
      }
      createMutation.mutate(body)
      return
    }

    // Edit: PATCH, carrying only what this admin actually changed.
    //
    // Every field on `ManagedAICredentialUpdate` reads an omitted value as
    // "leave it alone", so the diff below is lossless — and it is what stops a
    // rename from re-asserting a stale snapshot of the auto-provision fields
    // (see `openedWithRef`). Membership follows the same rule through
    // `membershipDirty`; api_key follows it by being blank unless typed.
    //
    // Values still carry their own clear-vs-set meaning once a field *is*
    // dirty: `available_models: []` clears the curation, `model_override_*:
    // ""` clears the pinned model.
    const opened = openedWithRef.current
    const sameSet = (a: string[], b: string[]) =>
      a.length === b.length &&
      [...a].sort().join("\u0000") === [...b].sort().join("\u0000")

    const body: ManagedAICredentialUpdate = {
      // `undefined` = leave membership exactly as it is. See `membershipDirty`.
      target_user_ids: membershipDirty ? targetUserIds : undefined,
    }
    if (data.name.trim() !== opened.name.trim()) {
      body.name = data.name.trim()
    }
    if (includesBaseUrl && (data.base_url ?? "") !== (opened.base_url ?? "")) {
      body.base_url = data.base_url?.trim() || null
    }
    if (includesModel && (data.model ?? "") !== (opened.model ?? "")) {
      body.model = data.model?.trim() || null
    }
    if ((data.default_model ?? "") !== (opened.default_model ?? "")) {
      // Blank still leaves `default_model` alone — the backend has no clear for
      // it, unlike the per-mode overrides.
      body.default_model = defaultModel || undefined
    }
    if ((data.available_models ?? "") !== (opened.available_models ?? "")) {
      body.available_models = availableModels
    }
    if (data.set_as_default !== opened.set_as_default) {
      body.set_as_default = data.set_as_default
    }
    if (data.set_user_sdk_defaults !== opened.set_user_sdk_defaults) {
      body.set_user_sdk_defaults = data.set_user_sdk_defaults
    }
    if (!sameSet(data.sdk_default_modes, opened.sdk_default_modes)) {
      body.sdk_default_modes = data.sdk_default_modes
    }
    if (!sameSet(data.auto_provision_roles, opened.auto_provision_roles)) {
      // `[]` is a real value here (stop auto-provisioning), not "no change" —
      // the backend distinguishes it from an omitted field, and existing
      // members keep their credential either way.
      body.auto_provision_roles = data.auto_provision_roles
    }
    if (
      overrideConversation !==
      stripProviderPrefix(opened.model_override_conversation ?? "")
    ) {
      body.model_override_conversation = overrideConversation
    }
    if (
      overrideBuilding !==
      stripProviderPrefix(opened.model_override_building ?? "")
    ) {
      body.model_override_building = overrideBuilding
    }
    if (data.api_key && data.api_key.trim() !== "") {
      body.api_key = data.api_key
    }
    updateMutation.mutate(body)
  }

  const dialogBody = (
    <DialogContent className="sm:max-w-lg max-h-[90vh] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>
          {mode === "create"
            ? "Provision LLM Provider Credential"
            : "Edit Managed Credential"}
        </DialogTitle>
        <DialogDescription>
          {mode === "create"
            ? "Create a read-only AI credential on behalf of one or more users. Each user receives an independent credential they can use and set as their default, but cannot edit or delete."
            : "Update this managed credential. Changes are reconciled to every member's credential. Add or remove members below."}
        </DialogDescription>
      </DialogHeader>
      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
          <FormField
            control={form.control}
            name="name"
            render={({ field }) => (
              <FormItem>
                <FormLabel>
                  Name <span className="text-destructive">*</span>
                </FormLabel>
                <FormControl>
                  <Input placeholder="Company Anthropic Key" {...field} />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />

          <FormField
            control={form.control}
            name="type"
            render={({ field }) => (
              <FormItem>
                <FormLabel>
                  Provider Type <span className="text-destructive">*</span>
                </FormLabel>
                <Select
                  onValueChange={field.onChange}
                  value={field.value}
                  disabled={mode === "edit"}
                >
                  <FormControl>
                    <SelectTrigger>
                      <SelectValue placeholder="Select a provider" />
                    </SelectTrigger>
                  </FormControl>
                  <SelectContent>
                    {PROVIDER_TYPE_OPTIONS.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <FormDescription>
                  {mode === "edit"
                    ? "Provider type can't be changed after creation."
                    : PROVIDER_TYPE_OPTIONS.find((o) => o.value === selectedType)
                        ?.description}
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />

          <FormField
            control={form.control}
            name="api_key"
            render={({ field }) => (
              <FormItem>
                <FormLabel>
                  API Key
                  {mode === "create" && <span className="text-destructive"> *</span>}
                </FormLabel>
                <FormControl>
                  <Input
                    type="password"
                    placeholder={
                      mode === "edit" ? "Leave blank to keep existing key" : "sk-..."
                    }
                    autoComplete="off"
                    {...field}
                    value={field.value ?? ""}
                  />
                </FormControl>
                <FormDescription>
                  {mode === "edit"
                    ? "Leave blank to keep the current key for all members."
                    : "Shared into each target user's independent credential row at rest."}
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />

          {showBaseUrl && (
            <FormField
              control={form.control}
              name="base_url"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>
                    Base URL
                    {selectedType === "openai_compatible" && (
                      <span className="text-destructive"> *</span>
                    )}
                  </FormLabel>
                  <FormControl>
                    <Input
                      placeholder="https://api.example.com/v1"
                      {...field}
                      value={field.value ?? ""}
                    />
                  </FormControl>
                  <FormDescription>
                    {selectedType === "google"
                      ? "Optional endpoint override for Google AI."
                      : "Endpoint for the OpenAI-compatible API."}
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />
          )}

          {showModel && (
            <FormField
              control={form.control}
              name="model"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>
                    Model <span className="text-destructive">*</span>
                  </FormLabel>
                  <FormControl>
                    <Input
                      placeholder="meta-llama/Llama-3-70b"
                      {...field}
                      value={field.value ?? ""}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
          )}

          <FormField
            control={form.control}
            name="default_model"
            render={({ field }) => (
              <FormItem>
                <div className="flex items-center justify-between">
                  <FormLabel>Default model</FormLabel>
                  {PROVIDER_MODELS_DOC_URL[selectedType] && (
                    <a
                      href={PROVIDER_MODELS_DOC_URL[selectedType]}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground hover:underline"
                    >
                      View available models
                      <ExternalLink className="h-3 w-3" />
                    </a>
                  )}
                </div>
                <FormControl>
                  <Input
                    placeholder="e.g. claude-sonnet-4-6"
                    {...field}
                    value={field.value ?? ""}
                  />
                </FormControl>
                <FormDescription>
                  The model used by default with this credential, across agents
                  and native apps. Leave blank to use the platform default. Use a
                  concrete model id for native apps.
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />

          <FormField
            control={form.control}
            name="available_models"
            render={({ field }) => (
              <FormItem>
                <div className="flex items-center justify-between">
                  <FormLabel>Available models</FormLabel>
                  <Button
                    type="button"
                    variant="link"
                    size="sm"
                    className="h-auto p-0 text-xs"
                    onClick={() => {
                      void fillTopModels()
                    }}
                    // Reuse the canTest() gating: fill needs to probe unless a
                    // fresh successful result is already available to reuse.
                    disabled={
                      testMutation.isPending ||
                      isPending ||
                      (!canTest() &&
                        !(testResult?.success && (testResult.models?.length ?? 0) > 0))
                    }
                  >
                    {testMutation.isPending ? (
                      <>
                        <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                        Testing...
                      </>
                    ) : (
                      "Fill top 10 models"
                    )}
                  </Button>
                </div>
                <FormControl>
                  <Textarea
                    rows={3}
                    placeholder="One model id per line (or comma-separated)"
                    {...field}
                    value={field.value ?? ""}
                  />
                </FormControl>
                <FormDescription>
                  Models offered for selection with this credential. Leave empty
                  to offer all auto-detected models.
                </FormDescription>
                <FormMessage />
              </FormItem>
            )}
          />

          <div className="space-y-2">
            <Label className="text-sm font-medium">Target Users</Label>
            <UserAllowlistPicker
              label={null}
              enabled={isOpen}
              selected={targets}
              searchPlaceholder="Search users to provision for..."
              emptyHint="Select the users to provision this credential for, or leave empty and pick roles under Auto-provision below."
              onAdd={(user) => {
                setMembershipDirty(true)
                setTargets((prev) =>
                  prev.some((t) => t.userId === user.id)
                    ? prev
                    : [
                        ...prev,
                        {
                          id: user.id,
                          userId: user.id,
                          fallbackLabel: user.full_name
                            ? `${user.full_name} <${user.email}>`
                            : user.email,
                        },
                      ],
                )
              }}
              onRemove={(item) => {
                setMembershipDirty(true)
                setTargets((prev) => prev.filter((t) => t.userId !== item.userId))
              }}
            />
            {/* Saving is what removes a member, and removing a member deletes
                their credential. Previously the "at least one target" guard
                made emptying the list impossible; now that an
                auto-provision-only record may legitimately have none, the
                consequence has to be visible before Save instead. */}
            {membershipDirty && removedMemberCount > 0 && (
              <p className="text-xs text-amber-600 dark:text-amber-500">
                Saving removes this credential from {removedMemberCount}{" "}
                {removedMemberCount === 1 ? "member" : "members"} and deletes{" "}
                {removedMemberCount === 1 ? "their copy" : "their copies"} of
                it.
              </p>
            )}
          </div>

          <FormField
            control={form.control}
            name="set_as_default"
            render={({ field }) => (
              <FormItem className="flex items-center justify-between rounded-md border p-3">
                <div className="space-y-0.5 pr-4">
                  <FormLabel>Set as default</FormLabel>
                  <FormDescription>
                    Make this each user's default credential for its provider type.
                  </FormDescription>
                </div>
                <FormControl>
                  <Switch checked={field.value} onCheckedChange={field.onChange} />
                </FormControl>
              </FormItem>
            )}
          />

          <FormField
            control={form.control}
            name="set_user_sdk_defaults"
            render={({ field }) => (
              <FormItem className="flex items-center justify-between rounded-md border p-3">
                <div className="space-y-0.5 pr-4">
                  <FormLabel>Set user SDK defaults</FormLabel>
                  <FormDescription>
                    Wire each user's conversation and building SDK defaults to this
                    credential.
                  </FormDescription>
                </div>
                <FormControl>
                  <Switch checked={field.value} onCheckedChange={field.onChange} />
                </FormControl>
              </FormItem>
            )}
          />

          {/* Per-mode detail for the switch above. Hidden while it is off:
              nothing in here has any effect then. */}
          {setUserSdkDefaults && (
            <div className="space-y-4 rounded-md border p-3">
              <div className="space-y-2">
                <Label className="text-sm font-medium">Modes to wire</Label>
                <div className="flex flex-wrap gap-x-6 gap-y-2">
                  {SDK_MODE_OPTIONS.map((option) => (
                    <div key={option.value} className="flex items-center gap-2">
                      <Checkbox
                        id={`${fieldId}-sdk-mode-${option.value}`}
                        checked={sdkModes.includes(option.value)}
                        onCheckedChange={(checked) =>
                          toggleInArray(
                            "sdk_default_modes",
                            option.value,
                            checked === true,
                          )
                        }
                      />
                      <Label
                        htmlFor={`${fieldId}-sdk-mode-${option.value}`}
                        className="text-sm font-normal"
                      >
                        {option.label}
                      </Label>
                    </div>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">
                  Only the ticked modes point at this credential. A mode
                  unticked later keeps whatever it already set — it stops being
                  managed here, it is not torn down.
                </p>
              </div>

              {SDK_MODE_OPTIONS.filter((option) =>
                sdkModes.includes(option.value),
              ).map((option) => (
                <ModelOverrideField
                  key={option.value}
                  form={form}
                  name={modelOverrideField(option.value)}
                  modeLabel={option.label}
                  storedValue={
                    record?.[modelOverrideField(option.value)] ?? null
                  }
                  probeModels={probeModelsForOverride}
                  probeDisabled={!canTest() || isPending}
                />
              ))}
            </div>
          )}

          <div className="space-y-3 rounded-md border p-3">
            <div className="space-y-0.5">
              <Label className="text-sm font-medium">
                Auto-provision for new users
              </Label>
              <p className="text-xs text-muted-foreground">
                Applied when an account is created; use Apply to existing users
                for current accounts.
              </p>
            </div>
            <div className="flex flex-wrap gap-x-6 gap-y-2">
              {USER_ROLE_OPTIONS.map((role) => (
                <div key={role.value} className="flex items-center gap-2">
                  <Checkbox
                    id={`${fieldId}-auto-provision-${role.value}`}
                    checked={autoProvisionRoles.includes(role.value)}
                    onCheckedChange={(checked) =>
                      toggleInArray(
                        "auto_provision_roles",
                        role.value,
                        checked === true,
                      )
                    }
                  />
                  <Label
                    htmlFor={`${fieldId}-auto-provision-${role.value}`}
                    className="text-sm font-normal"
                  >
                    {role.label}
                  </Label>
                </div>
              ))}
            </div>
            {conflict && (
              <Alert variant="destructive">
                <AlertTitle>Another credential owns that default</AlertTitle>
                <AlertDescription>
                  {describeAutoProvisionConflict(conflict)}
                </AlertDescription>
              </Alert>
            )}
          </div>

          {testResult && (
            <Alert variant={testResult.success ? "default" : "destructive"}>
              {testResult.success && <CheckCircle2 className="h-4 w-4 text-green-600" />}
              <AlertDescription>{describeTestResult(testResult)}</AlertDescription>
            </Alert>
          )}

          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline" type="button" disabled={isPending}>
                Cancel
              </Button>
            </DialogClose>
            <Button
              type="button"
              variant="secondary"
              onClick={() => testMutation.mutate()}
              disabled={!canTest() || isPending}
            >
              {testMutation.isPending ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Testing...
                </>
              ) : (
                "Test Connection"
              )}
            </Button>
            <LoadingButton type="submit" loading={isPending}>
              {mode === "create" ? "Provision" : "Save"}
            </LoadingButton>
          </DialogFooter>
        </form>
      </Form>
    </DialogContent>
  )

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        setIsOpen(open)
        if (!open) resetDialog()
      }}
    >
      {mode === "create" && (
        <DialogTrigger asChild>
          <Button>
            <Plus className="mr-2 h-4 w-4" />
            Provision Credential
          </Button>
        </DialogTrigger>
      )}
      {dialogBody}
    </Dialog>
  )
}
