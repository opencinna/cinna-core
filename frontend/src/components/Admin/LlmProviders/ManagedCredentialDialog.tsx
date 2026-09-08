import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { CheckCircle2, ExternalLink, Loader2, Plus } from "lucide-react"
import { useEffect, useId, useRef, useState } from "react"
import { useForm } from "react-hook-form"
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
import { ModelOverrideField } from "./ModelOverrideField"
import {
  getProviderTypeLabel,
  MANAGED_CREDENTIALS_QUERY_PREFIX,
  modelOverrideField,
  parseAvailableModels,
  providerModelsDocUrl,
  PROVIDER_TYPE_OPTIONS,
  sdkModeLabel,
  SDK_MODE_OPTIONS,
  SDK_MODE_VALUES,
  stripProviderPrefix,
} from "./providerTypes"

// Default credential name suggested for a freshly-provisioned record,
// derived from the selected provider type (e.g. "Anthropic Key").
function defaultCredentialName(
  type: AICredentialType,
  subject?: string,
): string {
  if (subject) return `${subject} — ${getProviderTypeLabel(type)}`
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
//
// Three fields this schema used to carry are gone rather than optional, and the
// generated types are why: `ManagedAICredentialCreate` / `Update` set
// `extra="forbid"`, so a client still sending `provisioning_mode`,
// `provider_admin_credential_id` or `auto_provision_roles` gets a 422 naming
// the field. A manual record is always `shared`, can never point at a provider,
// and is not a factory — the rule for who automatically receives a key lives on
// `ai_provider` and is edited under Providers.
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
  // Per-mode model pinned on the member's profile alongside the default
  // credential. Blank = the record has no opinion.
  model_override_conversation: z.string().optional(),
  model_override_building: z.string().optional(),
})

type FormData = z.infer<typeof baseFormSchema>

// Canonical Gemini alias used as the Google default — a stable pointer to the
// latest Flash model that may not appear verbatim in the discovered list.
const GOOGLE_DEFAULT_MODEL = "gemini-flash-latest"

// Pick the best default model id for the given provider type from a
// (prefix-stripped) model list:
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
  const parseKey = (id: string): { version: number[]; snapshot: number } => {
    const snapshotMatch = id.match(/(\d{6,})\s*$/)
    const snapshot = snapshotMatch ? Number(snapshotMatch[1]) : 0
    const body = snapshotMatch ? id.slice(0, snapshotMatch.index) : id
    const version = (body.match(/\d+/g) ?? []).map(Number)
    return { version: version.length > 0 ? version : [0], snapshot }
  }

  return sonnets.reduce((best, candidate) => {
    const a = parseKey(candidate)
    const b = parseKey(best)

    // Phase 1: compare version tokens element-by-element (missing slot = 0).
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
  model_override_conversation: "",
  model_override_building: "",
}

/**
 * Create-mode defaults, optionally named for the person the record is for.
 *
 * A fresh object every call: the ref below outlives the render, and the
 * module-level defaults must not become per-instance state.
 */
function createDefaults(nameSubject?: string): FormData {
  return {
    ...CREATE_DEFAULTS,
    name: defaultCredentialName(CREATE_DEFAULTS.type, nameSubject),
  }
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
    // Narrowed to the manual form's enum. A provider-owned record may carry a
    // type this form does not offer (a MiniMax provider creates one), which is
    // safe only because such a record never reaches this form — the
    // provider-owned branch returns before it is used.
    type: record.type as FormData["type"],
    api_key: "",
    base_url: record.base_url ?? "",
    model: record.model ?? "",
    default_model: record.default_model ?? "",
    available_models: (record.available_models ?? []).join("\n"),
    set_as_default: record.set_as_default ?? false,
    set_user_sdk_defaults: record.set_user_sdk_defaults ?? false,
    sdk_default_modes: record.sdk_default_modes ?? DEFAULT_SDK_MODES,
    model_override_conversation: record.model_override_conversation ?? "",
    model_override_building: record.model_override_building ?? "",
  }
}

/** "Fixed key" / "Per-user keys", from the record's computed mode. */
function keySourceLabel(record: ManagedAICredentialPublic): string {
  return record.provisioning_mode === "minted" ? "Per-user keys" : "Fixed key"
}

/** One label-over-value pair in the provider-owned branch's definition list. */
function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm">{value}</dd>
    </div>
  )
}

interface ManagedCredentialDialogProps {
  mode: "create" | "edit"
  // The record being edited (required for mode === "edit").
  record?: ManagedAICredentialPublic
  // Controlled open state — used by the actions menu in edit mode, and by the
  // invite wizard, which opens this dialog in create mode from its own button.
  // Omitted in create mode, the dialog supplies its own trigger and state.
  open?: boolean
  onOpenChange?: (open: boolean) => void
  // Create mode: members the record starts with. The invite wizard seeds the
  // person it has just invited, which is the whole reason its step 3 can exist
  // — the account is created by then, so there is an id to put in the picker.
  initialTargets?: UserAllowlistSelectedItem[]
  // Create mode: who this record is for, used to suggest a name. A per-person
  // key is a one-member record, and a fleet of them all called "Anthropic Key"
  // is the actual unreadability — not the row count.
  nameSubject?: string
  // Create mode: fired after the record is created, so a host surface can
  // record that the step is done.
  //
  // It is handed the reconcile result rather than nothing, because "the record
  // was created" and "that person can now work" are different facts and the
  // host has no business deriving the second from the first.
  onCreated?: (result: ManagedAICredentialReconcileResult) => void
}

/**
 * A managed AI credential — the key that exists, and who holds it.
 *
 * **Two compositions, one dialog**, because the record has two shapes and they
 * are not the same story:
 *
 * * **Manual** — the admin pasted a key and chose everything about it. The
 *   full form, eight blocks, as before minus the three concerns that moved:
 *   the key-source radio (a manual record is always `shared`), the provider
 *   organisation select (a manual record can never point at one) and the
 *   auto-provision roles (the rule lives on `ai_provider` now). All three are
 *   unsendable — `extra="forbid"` makes each a 422 naming the field — so they
 *   are gone rather than disabled.
 * * **Provider-owned** — a provider decided the key, the models and the SDK
 *   wiring, and the only editable thing here is *who holds it*. Those values
 *   render as **text**, not as disabled controls: that removes §4's
 *   "disabled controls with no explanation" anti-pattern structurally instead
 *   of annotating it, and it satisfies §9's accessibility requirement, since
 *   there is no disabled styling left to be the only carrier of meaning.
 */
export function ManagedCredentialDialog({
  mode,
  record,
  open: controlledOpen,
  onOpenChange,
  initialTargets,
  nameSubject,
  onCreated,
}: ManagedCredentialDialogProps) {
  // Controlled whenever a parent passes `open` — the actions menu in edit
  // mode, the invite wizard in create mode. Otherwise the dialog owns its own
  // state and renders its own trigger button.
  const isControlled = controlledOpen !== undefined
  const [internalOpen, setInternalOpen] = useState(false)
  const isOpen = isControlled ? controlledOpen : internalOpen
  const setIsOpen = (open: boolean) => {
    if (isControlled) onOpenChange?.(open)
    else setInternalOpen(open)
  }

  // Every row of the credentials table mounts one of these dialogs, so the
  // checkbox ids below have to be unique per instance even though Radix only
  // keeps the open one's content in the DOM.
  const fieldId = useId()

  const [targets, setTargets] = useState<UserAllowlistSelectedItem[]>(() =>
    mode === "edit" ? membersToTargets(record) : (initialTargets ?? []),
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

  // The branch. A provider-owned record's key and wiring are the provider's;
  // this dialog manages its membership and says where the rest is decided.
  const isProviderOwned = mode === "edit" && record?.is_provider_owned === true

  const form = useForm<FormData>({
    resolver: zodResolver(buildFormSchema(mode)),
    mode: "onBlur",
    defaultValues:
      mode === "edit" && record
        ? recordToFormData(record)
        : createDefaults(nameSubject),
  })

  // The values the form was last seeded with — i.e. what the record looked
  // like when this dialog opened. Every PATCH field is diffed against this,
  // for the same reason `membershipDirty` exists: the payload is *absolute*,
  // the snapshot is stale the moment it is taken, and resubmitting an
  // untouched field asserts a value the admin never chose.
  const openedWithRef = useRef<FormData>(
    mode === "edit" && record ? recordToFormData(record) : createDefaults(nameSubject),
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

  const toggleInArray = (
    field: "sdk_default_modes",
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
  const autoNameRef = useRef(defaultCredentialName(CREATE_DEFAULTS.type, nameSubject))

  // Keep the suggested name in sync with the selected type until the user
  // types their own name. Disabled in edit mode (type is immutable).
  useEffect(() => {
    if (mode === "edit") return
    const currentName = form.getValues("name")
    if (currentName === "" || currentName === autoNameRef.current) {
      const suggested = defaultCredentialName(selectedType, nameSubject)
      autoNameRef.current = suggested
      form.setValue("name", suggested)
    }
  }, [selectedType, form, mode, nameSubject])

  // Re-seed the form + picker whenever the edit dialog opens for a record, so a
  // stale local edit from a previously-closed dialog never leaks in.
  useEffect(() => {
    if (mode === "edit" && isOpen && record) {
      seedForm(recordToFormData(record))
      setTargets(membersToTargets(record))
      setMembershipDirty(false)
      setTestResult(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, record?.id])

  const resetDialog = () => {
    if (mode === "edit" && record) {
      seedForm(recordToFormData(record))
      setTargets(membersToTargets(record))
    } else {
      const defaults = createDefaults(nameSubject)
      seedForm(defaults)
      autoNameRef.current = defaults.name
      setTargets(initialTargets ?? [])
    }
    setMembershipDirty(false)
    setTestResult(null)
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
  // never asked for, reporting on a probe they did not run.
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
  // once it resolves. A failed/empty test fills nothing — the inline
  // test-result alert explains why.
  const fillTopModels = async () => {
    let result = testResult
    if (!(result?.success && (result.models?.length ?? 0) > 0)) {
      try {
        result = await testMutation.mutateAsync()
      } catch {
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

    // Per-user warnings: blocked and skipped (unknown / inactive). One toast per
    // entry so the admin sees each by name. `b.message` is the server's own
    // sentence for `b.reason`; this renders it rather than substituting one.
    for (const b of blocked) {
      showErrorToast(`${labelFor(b.user_id)} was not removed. ${b.message}`)
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

  const createMutation = useMutation({
    mutationFn: (body: ManagedAICredentialCreate) =>
      AdminLlmProvidersService.createManagedAiCredential({ requestBody: body }),
    onSuccess: (result) => {
      surfaceReconcileResult(result, targets)
      resetDialog()
      setIsOpen(false)
      onCreated?.(result)
    },
    onError: (error) => handleError.call(showErrorToast, error as ApiError),
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
    onError: (error) => handleError.call(showErrorToast, error as ApiError),
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

  // The provider-owned branch's one write: membership, and nothing else. Every
  // other field on the update model is refused on such a record with a 400
  // naming the provider, so none of them is sent.
  const saveMembersOnly = () => {
    updateMutation.mutate({
      target_user_ids: membershipDirty ? targets.map((t) => t.userId) : undefined,
    })
  }

  const onSubmit = (data: FormData) => {
    // Create only. A brand-new record with no members would do nothing at all,
    // so it is refused. An auto-provisioning record is no longer creatable
    // here at all — that shape is a provider.
    if (mode === "create" && targets.length === 0) {
      showErrorToast("Select at least one target user.")
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
    // "leave it alone", so the diff below is lossless. Membership follows the
    // same rule through `membershipDirty`; api_key follows it by being blank
    // unless typed.
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
      // `""` is a real clear, exactly as on the provider endpoint: `update`
      // writes the field whenever it is not null and `_normalize_default_model`
      // turns a blank into NULL. Sending `undefined` here instead dropped the
      // key from the body, so blanking the box saved green and changed nothing.
      body.default_model = defaultModel
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

  const membershipPicker = (
    <div className="space-y-2">
      <Label className="text-sm font-medium">
        {isProviderOwned ? "Who holds it" : "Target Users"}
      </Label>
      <UserAllowlistPicker
        label={null}
        enabled={isOpen}
        selected={targets}
        searchPlaceholder="Search users to provision for..."
        emptyHint="Select the users this credential is for."
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
      {/* Saving is what removes a member, and removing a member deletes their
          credential — so the consequence is visible before Save. */}
      {membershipDirty && removedMemberCount > 0 && (
        <p className="text-xs text-warning">
          Saving removes this credential from {removedMemberCount}{" "}
          {removedMemberCount === 1 ? "member" : "members"} and deletes{" "}
          {removedMemberCount === 1 ? "their copy" : "their copies"} of it.
        </p>
      )}
    </div>
  )

  // ── The provider-owned branch: four blocks, no form controls but one ────
  if (isProviderOwned && record) {
    const providerName = record.provider_name ?? "its provider"
    const wiredModes = record.set_user_sdk_defaults
      ? (record.sdk_default_modes ?? []).map(sdkModeLabel).join(", ")
      : ""
    const overrides = SDK_MODE_OPTIONS.filter(
      (option) => record[modelOverrideField(option.value)],
    )
      .map(
        (option) =>
          `${option.label}: ${record[modelOverrideField(option.value)]}`,
      )
      .join(" · ")

    return (
      <Dialog open={isOpen} onOpenChange={setIsOpen}>
        <DialogContent className="sm:max-w-lg max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{record.name}</DialogTitle>
            <DialogDescription>Managed by {providerName}.</DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <Alert>
              <AlertTitle>Managed by the provider {providerName}</AlertTitle>
              <AlertDescription>
                <p>
                  The key, the models and the SDK wiring are set on the
                  provider. Change them there. Deleting this credential means
                  deleting its provider.
                </p>
                <Button asChild variant="link" className="h-auto px-0">
                  <Link to="/admin/ai-credentials" hash="providers">
                    Open {providerName}
                  </Link>
                </Button>
              </AlertDescription>
            </Alert>

            {/* Values as text, not as disabled inputs. They are computed
                through the policy resolver on the wire, so this block cannot
                disagree with the provider. */}
            <div>
              <h3 className="text-sm font-medium">What the provider decided</h3>
              <dl className="mt-2 grid grid-cols-2 gap-3">
                <Fact
                  label="Type"
                  value={getProviderTypeLabel(record.type)}
                />
                <Fact label="Key source" value={keySourceLabel(record)} />
                <Fact
                  label="Default model"
                  value={record.default_model || "Platform default"}
                />
                <Fact
                  label="SDK defaults"
                  value={wiredModes || "Not wired"}
                />
                <Fact label="Model overrides" value={overrides || "None"} />
              </dl>
            </div>

            {membershipPicker}
          </div>

          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline" type="button" disabled={isPending}>
                Cancel
              </Button>
            </DialogClose>
            <LoadingButton
              type="button"
              loading={isPending}
              onClick={saveMembersOnly}
            >
              Save
            </LoadingButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  }

  // ── The manual branch: the full form ────────────────────────────────────
  const dialogBody = (
    <DialogContent className="sm:max-w-lg max-h-[90vh] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>
          {mode === "create"
            ? "Provision an AI credential"
            : "Edit managed credential"}
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
                  Type <span className="text-destructive">*</span>
                </FormLabel>
                <Select
                  onValueChange={field.onChange}
                  value={field.value}
                  disabled={mode === "edit"}
                >
                  <FormControl>
                    <SelectTrigger>
                      <SelectValue placeholder="Select a type" />
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
                    ? "The type can't be changed after creation."
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
                  {providerModelsDocUrl(selectedType) && (
                    <a
                      href={providerModelsDocUrl(selectedType)}
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
                  and native apps. Leave blank to use the platform default.
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

          {membershipPicker}

          <FormField
            control={form.control}
            name="set_as_default"
            render={({ field }) => (
              <FormItem className="flex items-center justify-between rounded-md border p-3">
                <div className="space-y-0.5 pr-4">
                  <FormLabel>Set as default</FormLabel>
                  <FormDescription>
                    Make this each user's default credential for its type.
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
              ).map((option) => {
                const name = modelOverrideField(option.value)
                return (
                  <ModelOverrideField
                    key={option.value}
                    id={`${fieldId}-override-${option.value}`}
                    modeLabel={option.label}
                    value={form.watch(name) ?? ""}
                    onChange={(next) =>
                      form.setValue(name, next, { shouldDirty: true })
                    }
                    storedValue={record?.[name] ?? null}
                    probeModels={probeModelsForOverride}
                    probeDisabled={!canTest() || isPending}
                  />
                )
              })}
            </div>
          )}

          {/* The rule moved to the provider, and an admin who cannot find the
              control it replaced needs to be told where it went — deleting it
              silently is how they conclude the feature was removed. */}
          <p className="text-xs text-muted-foreground">
            Who automatically receives a key is set on a provider, under{" "}
            <Link
              to="/admin/ai-credentials"
              hash="providers"
              className="text-primary hover:underline"
            >
              Providers
            </Link>
            .
          </p>

          {testResult && (
            <Alert variant={testResult.success ? "default" : "destructive"}>
              {testResult.success && <CheckCircle2 className="h-4 w-4" />}
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
      {/* Only when this dialog owns its own open state. A controlled create —
          the invite wizard's hand-off — brings its own trigger. */}
      {mode === "create" && !isControlled && (
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
