import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { CheckCircle2, ExternalLink, Loader2, Plus } from "lucide-react"
import { useEffect, useRef, useState } from "react"
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
import {
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
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
import {
  getProviderTypeLabel,
  MANAGED_CREDENTIALS_QUERY_PREFIX,
  PROVIDER_TYPE_OPTIONS,
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
  }
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

  const [targets, setTargets] = useState<UserAllowlistSelectedItem[]>(() =>
    membersToTargets(record),
  )
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const form = useForm<FormData>({
    resolver: zodResolver(buildFormSchema(mode)),
    mode: "onBlur",
    defaultValues: mode === "edit" && record ? recordToFormData(record) : CREATE_DEFAULTS,
  })

  const selectedType = form.watch("type") as AICredentialType
  const selectedApiKey = form.watch("api_key")
  const selectedBaseUrl = form.watch("base_url")
  const showBaseUrl = selectedType === "openai_compatible" || selectedType === "google"
  const showModel = selectedType === "openai_compatible"

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
      form.reset(recordToFormData(record))
      setTargets(membersToTargets(record))
      setTestResult(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, record?.id])

  const resetDialog = () => {
    if (mode === "edit" && record) {
      form.reset(recordToFormData(record))
      setTargets(membersToTargets(record))
    } else {
      form.reset(CREATE_DEFAULTS)
      autoNameRef.current = CREATE_DEFAULTS.name
      setTargets([])
    }
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

  const createMutation = useMutation({
    mutationFn: (body: ManagedAICredentialCreate) =>
      AdminLlmProvidersService.createManagedAiCredential({ requestBody: body }),
    onSuccess: (result) => {
      surfaceReconcileResult(result, targets)
      resetDialog()
      setIsOpen(false)
    },
    onError: handleError.bind(showErrorToast),
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
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX })
    },
  })

  const isPending = createMutation.isPending || updateMutation.isPending

  const onSubmit = (data: FormData) => {
    if (targets.length === 0) {
      showErrorToast("Select at least one target user.")
      return
    }

    const includesBaseUrl = data.type === "openai_compatible" || data.type === "google"
    const includesModel = data.type === "openai_compatible"
    const targetUserIds = targets.map((t) => t.userId)

    const defaultModel = stripProviderPrefix(data.default_model ?? "")
    const availableModels = parseAvailableModels(data.available_models)

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
      }
      createMutation.mutate(body)
      return
    }

    // Edit: PATCH with the picker selection as the desired membership. Omit
    // api_key when blank so the stored key is kept for all members.
    //
    // Curation clear-vs-no-change semantics mirror base_url/model:
    //  - available_models: [] explicitly clears curation (the textarea is
    //    seeded from the record, so a blank textarea on edit means "clear").
    //  - default_model: the trimmed/stripped value (blank → undefined leaves it
    //    unchanged, matching the backend's None=no-change for default_model).
    const body: ManagedAICredentialUpdate = {
      name: data.name.trim(),
      base_url: includesBaseUrl ? data.base_url?.trim() || null : null,
      model: includesModel ? data.model?.trim() || null : null,
      default_model: defaultModel || undefined,
      available_models: availableModels,
      target_user_ids: targetUserIds,
      set_as_default: data.set_as_default,
      set_user_sdk_defaults: data.set_user_sdk_defaults,
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
            <Label className="text-sm font-medium">
              Target Users <span className="text-destructive">*</span>
            </Label>
            <UserAllowlistPicker
              label={null}
              enabled={isOpen}
              selected={targets}
              searchPlaceholder="Search users to provision for..."
              emptyHint="Select one or more users to provision this credential for."
              onAdd={(user) =>
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
              }
              onRemove={(item) =>
                setTargets((prev) => prev.filter((t) => t.userId !== item.userId))
              }
            />
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
