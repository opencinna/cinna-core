import type { AICredentialType, ManagedAICredentialPublic } from "@/client"
import { ApiError } from "@/client/core/ApiError"
import { userRoleLabel } from "@/utils/userRoles"

// Display metadata for the user-selectable AI credential provider types.
// Mirrors the copy used in the user-facing AICredentialDialog so admin and
// user surfaces stay consistent.
// NOTE: MiniMax is temporarily disabled in the UI (not currently supported).
export const PROVIDER_TYPE_OPTIONS: {
  value: AICredentialType
  label: string
  description: string
}[] = [
  { value: "anthropic", label: "Anthropic", description: "Claude AI models (API Key or OAuth Token)" },
  { value: "openai", label: "OpenAI", description: "OpenAI API (GPT-4o, o3, etc.)" },
  { value: "openai_compatible", label: "OpenAI Compatible", description: "OpenAI-compatible endpoints (vLLM, custom)" },
  { value: "google", label: "Google", description: "Google AI (Gemini models via AI Studio)" },
]

const PROVIDER_TYPE_LABELS: Partial<Record<AICredentialType, string>> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  openai_compatible: "OpenAI Compatible",
  google: "Google",
}

export function getProviderTypeLabel(type: AICredentialType): string {
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
export const MANAGED_CREDENTIALS_QUERY_PREFIX = ["admin", "llm-providers"] as const

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

// ── Auto-provision conflicts ───────────────────────────────────────────

/**
 * The 409 body raised when two managed credentials would both wire the same
 * `(role, mode)` default slot. Mirrors `_conflict_409` in
 * `backend/app/api/routes/admin_llm_providers.py`.
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
    `${sdkModeLabel(conflict.mode).toLowerCase()} default for auto-provisioned ` +
    `${userRoleLabel(conflict.role)} accounts. Only one credential can own a ` +
    `role's default for a mode — drop the role or the mode on one of the two.`
  )
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
 * Returns the record that would collide, or `null`.
 */
export function findAutoProvisionConflict(
  records: ManagedAICredentialPublic[],
  record: ManagedAICredentialPublic,
  role: string,
): ManagedAICredentialPublic | null {
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
