import type { AICredentialType } from "@/client"
import { ApiError } from "@/client/core/ApiError"
import { userRoleLabel } from "@/utils/userRoles"

// The types a **manual** managed credential may be created as, with the copy
// the user-facing AICredentialDialog uses, so the admin and user surfaces stay
// consistent.
//
// This is a *selection* list and is deliberately shorter than the adapter
// registry: MiniMax is not offered here. It is **not** a label source — see
// `PROVIDER_TYPE_LABELS` below, which must cover every type the server can
// send, including the ones this list declines to offer.
export const PROVIDER_TYPE_OPTIONS: {
  value: AICredentialType
  label: string
  description: string
}[] = [
  {
    value: "anthropic",
    label: "Anthropic",
    description: "Claude AI models (API Key or OAuth Token)",
  },
  {
    value: "openai",
    label: "OpenAI",
    description: "OpenAI API (GPT-4o, o3, etc.)",
  },
  {
    value: "openai_compatible",
    label: "OpenAI Compatible",
    description: "OpenAI-compatible endpoints (vLLM, custom)",
  },
  {
    value: "google",
    label: "Google",
    description: "Google AI (Gemini models via AI Studio)",
  },
]

/**
 * How each vendor is spelled for a reader.
 *
 * A **total** `Record` over the generated union, not a `Partial`: a sixth type
 * added on the server stops compiling here instead of rendering as a raw
 * identifier. It was `Partial` and missing `minimax`, whose adapter *is*
 * registered — so a MiniMax provider read "minimax" on the Access tab and in
 * the invite checklist while reading "MiniMax" on the Providers tab, which is
 * the same defect class as an unmapped skip reason printing its own token.
 *
 * The adapters endpoint stays the authority wherever it is already in hand
 * (`useProviderAdapters().typeLabel`); this map is for the surfaces that would
 * otherwise have to open a query just to spell a word, and for the first paint
 * before that query answers.
 */
const PROVIDER_TYPE_LABELS: Record<AICredentialType, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  openai_compatible: "OpenAI Compatible",
  google: "Google",
  minimax: "MiniMax",
}

export function getProviderTypeLabel(type: AICredentialType): string {
  // The `Record` above is the drift-catcher and stays that way. This fallback
  // is for the other case — a value off the wire outside the union, which the
  // compiler cannot rule out.
  return PROVIDER_TYPE_LABELS[type] ?? type
}

// Shared React Query key prefix for all managed-credential queries. Mutations
// invalidate by this prefix so the centralized list query (and any scoped
// variant) refetch together.
//
// The `"llm-providers"` segment is NOT the route path and does not follow the
// page's rename to `/admin/ai-credentials`: it is a cache key, and three
// surfaces (the page, the auto-provision matrix and the invite wizard) depend
// on producing the *same* string. Renaming it splits the cache silently — no
// error, just two lists that stop agreeing with each other.
export const MANAGED_CREDENTIALS_QUERY_PREFIX = [
  "admin",
  "llm-providers",
] as const

/**
 * React Query key for the connected **providers** (`/admin/ai-providers`).
 *
 * Read by the Providers tab, the create/edit surfaces, the invite wizard's
 * provisioning step and the Access tab's Company AI providers card. Every
 * provider mutation invalidates this *and* `MANAGED_CREDENTIALS_QUERY_PREFIX`:
 * a policy edit re-applies to the members of the credential the provider owns,
 * so the managed list is stale too.
 *
 * It replaced `PROVIDER_ADMIN_CREDENTIALS_QUERY_KEY`, which addressed the
 * retired `/admin/provider-admin-credentials` router.
 */
export const AI_PROVIDERS_QUERY_KEY = ["admin", "ai-providers"] as const

// React Query key for the fleet-wide managed-credential list, optionally
// scoped to a single target user.
export function managedCredentialsQueryKey(targetUserId?: string | null) {
  return [...MANAGED_CREDENTIALS_QUERY_PREFIX, targetUserId ?? "all"] as const
}

// ── SDK default modes ──────────────────────────────────────────────────
// The two slots a managed credential can claim on a member's profile
// (`default_ai_credential_<mode>_id` + its model override). The values are the
// backend's `sdk_default_modes` strings.
export const SDK_MODE_OPTIONS: { value: string; label: string }[] = [
  { value: "conversation", label: "Conversation" },
  { value: "building", label: "Building" },
]

/** The mode identifiers alone, in the order they are presented. */
export const SDK_MODE_VALUES: string[] = SDK_MODE_OPTIONS.map(
  (option) => option.value,
)

const KNOWN_SDK_MODES = new Set(SDK_MODE_VALUES)

/**
 * The recognised modes out of a `sdk_default_modes` list off the wire.
 *
 * `sdk_default_modes` is a JSON string list with no server-side enum behind
 * it, so an unrecognised entry is reachable — and every surface that acts on
 * the list has to drop those the same way the backend does, because a mode
 * that is not one of these two writes nothing at all (`_MODE_POINTER_ATTR` /
 * `_MODE_OVERRIDE_ATTR` in `managed_ai_credentials_service.py`). Naming a
 * phantom mode in the confirm dialog's copy, or treating one as a conflict,
 * both come from re-deriving that filter by hand; this is the one place it
 * is written.
 */
export function knownSdkModes(
  modes: readonly string[] | null | undefined,
): string[] {
  return (modes ?? []).filter((mode) => KNOWN_SDK_MODES.has(mode))
}

export function sdkModeLabel(mode: string): string {
  return SDK_MODE_OPTIONS.find((option) => option.value === mode)?.label ?? mode
}

/**
 * The form/record field carrying a mode's model override.
 *
 * Mirrors the backend's `_MODE_OVERRIDE_ATTR`. The mapping is trivial and was
 * therefore inlined at each use, which is exactly how the two halves of a
 * ternary end up disagreeing about which mode is which.
 */
export function modelOverrideField(
  mode: string,
): "model_override_conversation" | "model_override_building" {
  return mode === "conversation"
    ? "model_override_conversation"
    : "model_override_building"
}

// ── Model ids ──────────────────────────────────────────────────────────

/**
 * Strip any leading `provider/` prefix for nicer display.
 *
 * The backend re-normalises regardless; doing it here keeps the box showing
 * the value that will actually be stored.
 */
export function stripProviderPrefix(value: string): string {
  const trimmed = value.trim()
  const idx = trimmed.indexOf("/")
  return idx >= 0 ? trimmed.slice(idx + 1) : trimmed
}

/**
 * Parse the free-form available-models textarea into a deduped,
 * prefix-stripped list. Accepts commas and newlines as separators.
 */
export function parseAvailableModels(raw: string | undefined): string[] {
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

// Official "available models" documentation page per type. `openai_compatible`
// has no canonical page (the model list depends on the configured endpoint),
// so it is intentionally absent and the link is omitted for that type.
const PROVIDER_MODELS_DOC_URL: Partial<Record<AICredentialType, string>> = {
  anthropic: "https://platform.claude.com/docs/en/about-claude/models/overview",
  google: "https://ai.google.dev/gemini-api/docs/models",
  openai: "https://developers.openai.com/api/docs/models",
}

/** The vendor's own model list page, or `undefined` when it has none. */
export function providerModelsDocUrl(
  type: AICredentialType | undefined,
): string | undefined {
  return type ? PROVIDER_MODELS_DOC_URL[type] : undefined
}

// ── Auto-provision conflicts ───────────────────────────────────────────

/**
 * The 409 body raised when two **providers** would both wire the same
 * `(role, mode)` default slot. Mirrors `_conflict_409` in
 * `backend/app/api/routes/admin_ai_providers.py`.
 *
 * The wire keys still say `credential`. That is deliberate and is Phase 4's
 * decision, not an oversight: the envelope predates the provider/credential
 * split and renaming it would break every reader for no gain. Only what a
 * person *reads* changed — see `describeAutoProvisionConflict`, which says
 * "provider", because after §2.1 that is what owns the rule.
 */
export interface AutoProvisionConflict {
  message: string
  conflicting_credential_id: string
  conflicting_credential_name: string
  role: string
  mode: string
}

/**
 * Pull the structured conflict out of an error, or `null` if it is anything
 * else.
 *
 * Every field is checked rather than trusting the `code`: a 409 from a future
 * release that carries the code but not the names would otherwise render a
 * sentence with `undefined` in it, which is worse than falling back to the
 * generic error toast.
 */
export function parseAutoProvisionConflict(
  error: unknown,
): AutoProvisionConflict | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const detail = (error.body as { detail?: unknown } | undefined)?.detail
  if (!detail || typeof detail !== "object") return null
  const body = detail as Record<string, unknown>
  if (body.code !== "auto_provision_conflict") return null
  if (
    typeof body.conflicting_credential_id !== "string" ||
    typeof body.conflicting_credential_name !== "string" ||
    typeof body.role !== "string" ||
    typeof body.mode !== "string"
  ) {
    return null
  }
  return {
    message:
      typeof body.message === "string"
        ? body.message
        : "Another credential already claims this default.",
    conflicting_credential_id: body.conflicting_credential_id,
    conflicting_credential_name: body.conflicting_credential_name,
    role: body.role,
    mode: body.mode,
  }
}

/**
 * The conflict in the vocabulary of the admin looking at it.
 *
 * The backend message says the same thing with raw identifiers
 * (`agent-developer`); every label on these screens is the display form, so it
 * is recomposed here rather than passed through.
 */
export function describeAutoProvisionConflict(
  conflict: AutoProvisionConflict,
): string {
  return (
    `"${conflict.conflicting_credential_name}" already sets the ` +
    `${sdkModeLabel(conflict.mode)} default for auto-provisioned ` +
    `${userRoleLabel(conflict.role)} accounts. Only one provider can own a ` +
    `role's default for a mode — drop the role, or turn off that mode, on one ` +
    `of the two.`
  )
}

/**
 * The parts of a record this rule reads.
 *
 * Structural rather than `AIProviderPublic` so the helper cannot acquire a
 * dependency on fields it does not use. It is written against providers —
 * `auto_provision_roles` moved there and is no longer on
 * `ManagedAICredentialPublic` at all — but the rule is about the four fields,
 * not about the table.
 */
export interface AutoProvisionSlotClaimant {
  id: string
  name: string
  set_user_sdk_defaults?: boolean | null
  sdk_default_modes?: readonly string[] | null
  auto_provision_roles?: readonly string[] | null
}

/**
 * Advisory, client-side answer to "would ticking this role here be refused?".
 *
 * The same rule the backend enforces in `_validate_auto_provision_uniqueness`,
 * evaluated against the list already on screen so a doomed checkbox can be
 * marked before it is clicked. It is a hint, not a gate: the list can be
 * stale, and the server stays the authority — the click still goes out and a
 * real 409 is still rendered.
 *
 * Returns the provider that would collide, or `null`.
 */
export function findAutoProvisionConflict<T extends AutoProvisionSlotClaimant>(
  records: readonly T[],
  record: AutoProvisionSlotClaimant,
  role: string,
): T | null {
  if (!record.set_user_sdk_defaults) return null
  // Only the two real slots count, exactly as the backend filters them — an
  // unrecognised mode string on both records is not a collision because
  // neither of them writes anything for it.
  const modes = new Set(knownSdkModes(record.sdk_default_modes))
  if (modes.size === 0) return null
  return (
    records.find(
      (other) =>
        other.id !== record.id &&
        other.set_user_sdk_defaults &&
        (other.auto_provision_roles ?? []).includes(role) &&
        (other.sdk_default_modes ?? []).some((mode) => modes.has(mode)),
    ) ?? null
  )
}

// ── Provider kinds ─────────────────────────────────────────────────────

/**
 * What a provider *is*, in the two sentences §9 fixes.
 *
 * `label` is the word an admin scans a list by; `blurb` is why they would pick
 * it. The second sentence on `minted` is not decoration: per-user spend
 * tracking is the reason that kind exists, so the surface offering it says so.
 */
export const AI_PROVIDER_KINDS: {
  value: "fixed_key" | "minted"
  label: string
  blurb: string
}[] = [
  {
    value: "fixed_key",
    label: "Fixed key",
    blurb: "Everyone shares one key you paste.",
  },
  {
    value: "minted",
    label: "Per-user keys",
    blurb:
      "Each person gets their own, created in your provider account. This is what makes per-user spend tracking possible.",
  },
]

/** Display label for a provider `kind` string off the wire. */
export function aiProviderKindLabel(kind: string): string {
  return AI_PROVIDER_KINDS.find((entry) => entry.value === kind)?.label ?? kind
}

// One blocked member from a 409 removal response — the record's delete and the
// keys list's per-key revoke send the same envelope.
//
// `message` is the server's sentence for `reason`, and it is what gets
// rendered. The record menu used to state its own — "in use by a published
// bundle" — for every block, including `mint_in_flight`, where the sentence is
// wrong and the remedy it implies (force) is the one thing an admin must not
// reach for while a key is being minted. Shared from here so the second reader
// cannot reintroduce that.
export interface BlockedMember {
  user_id: string
  reason: string
  message: string
  impact?: unknown
}

// Extract the blocked-members list from a 409 ApiError body
// ({ detail: { message, blocked: [...] } }).
export function blockedFromError(error: unknown): BlockedMember[] | null {
  if (error instanceof ApiError && error.status === 409) {
    const detail = (error.body as { detail?: unknown } | undefined)?.detail
    if (detail && typeof detail === "object" && "blocked" in detail) {
      const blocked = (detail as { blocked?: unknown }).blocked
      if (Array.isArray(blocked)) return blocked as BlockedMember[]
    }
  }
  return null
}
