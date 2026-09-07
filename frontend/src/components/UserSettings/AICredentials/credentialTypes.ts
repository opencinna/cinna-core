import type {
  AICredentialPublic,
  AICredentialTestResult,
  AICredentialType,
} from "@/client"

/**
 * The tables and helpers shared by the AI Credentials surfaces.
 *
 * They used to sit at the top of a 1027-line `AICredentials.tsx`; the split
 * that produced this folder made them a module so the card, the row, the two
 * mode dialogs and the wizard all read the same provider names, the same
 * compatibility matrix and the same expiry rule.
 */

/** SDK engines an environment can run on. */
export const SDK_ENGINE_OPTIONS = [
  { value: "claude-code", label: "Claude Code" },
  { value: "opencode", label: "OpenCode" },
]

/**
 * Which credential types each SDK engine supports.
 *
 * Hardcoded here because the backend enforces a strict `engine/type` match at
 * validation time but exposes no map (contract gap 3 in the specification):
 * this table can silently drift from the server's rule.
 */
export const SDK_CREDENTIAL_COMPATIBILITY: Record<string, string[]> = {
  "claude-code": ["anthropic"],
  opencode: ["anthropic", "openai", "openai_compatible", "google"],
}

/** Model ids offered as `datalist` suggestions per credential type. */
export const SUGGESTED_MODELS: Record<string, string[]> = {
  anthropic: ["claude-opus-4", "claude-sonnet-4-5", "claude-haiku-4-5"],
  openai: ["gpt-4o", "gpt-4o-mini", "o3", "o4-mini"],
  google: ["gemini-2.5-pro", "gemini-2.5-flash"],
  openai_compatible: [],
}

/** Full SDK id stored when no concrete credential pins the provider. */
export const DEFAULT_SDK_FOR_ENGINE: Record<string, string> = {
  "claude-code": "claude-code/anthropic",
  opencode: "opencode/anthropic",
}

/** Sentinel for the "Use Default" credential selection. */
export const USE_DEFAULT_SENTINEL = "__default__"

/**
 * Total, not `Partial`: every member of the generated union needs a name here,
 * because a missing key does not fail — it prints the raw enum id into a row's
 * metadata line, a tooltip and two dialog labels. `minimax` is the one that
 * proved it. Adding a provider to the backend now breaks the typecheck, which
 * is the only thing that reliably notices.
 *
 * This is not the same list as `TYPE_OPTIONS`: the wizard must not offer
 * MiniMax (temporarily unsupported), but rows that already hold one still have
 * to be named.
 */
export const TYPE_DISPLAY_NAMES: Record<AICredentialType, string> = {
  anthropic: "Anthropic",
  openai_compatible: "OpenAI Compatible",
  openai: "OpenAI",
  google: "Google AI",
  minimax: "MiniMax",
}

/**
 * The provider choices the Add wizard offers, with the one-line description
 * each pill carries as its tooltip.
 *
 * NOTE: MiniMax is temporarily disabled in the UI (not currently supported).
 */
export const TYPE_OPTIONS: {
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

/** Name suggested for a fresh credential, derived from its provider. */
export function defaultCredentialName(type: AICredentialType): string {
  const label = TYPE_OPTIONS.find((o) => o.value === type)?.label ?? type
  return `${label} Key`
}

export function extractEngine(sdkId: string | null | undefined): string {
  if (!sdkId) return "claude-code"
  return sdkId.includes("/") ? sdkId.split("/")[0] : sdkId
}

export function composeSDKId(
  engine: string,
  credentialType: string | null,
): string {
  if (credentialType) return `${engine}/${credentialType}`
  return DEFAULT_SDK_FOR_ENGINE[engine] ?? `${engine}/anthropic`
}

export function getCompatibleCredentials(
  engine: string,
  credentials: AICredentialPublic[],
): AICredentialPublic[] {
  const compatible = SDK_CREDENTIAL_COMPATIBILITY[engine] ?? []
  return credentials.filter((c) => compatible.includes(c.type))
}

export function getTypeDisplayName(type: AICredentialType): string {
  return TYPE_DISPLAY_NAMES[type] || type
}

export function getEngineLabel(engine: string): string {
  return SDK_ENGINE_OPTIONS.find((o) => o.value === engine)?.label ?? engine
}

export interface ModeSummary {
  engine: string
  credential: string
  model?: string
}

export function buildModeSummary(
  engine: string,
  credentialId: string,
  modelOverride: string,
  credentials: AICredentialPublic[],
  resolvedDefault: AICredentialPublic | null | undefined,
): ModeSummary {
  let credential: string
  if (credentialId === USE_DEFAULT_SENTINEL) {
    credential = resolvedDefault
      ? `Default (${resolvedDefault.name})`
      : "Default"
  } else {
    const cred = credentials.find((c) => c.id === credentialId)
    credential = cred?.name ?? "Unknown"
  }

  return {
    engine: getEngineLabel(engine),
    credential,
    model: modelOverride || undefined,
  }
}

/** Days from now until `date`, floored — negative once the date has passed. */
function daysUntil(date: Date): number {
  return Math.floor((date.getTime() - Date.now()) / (1000 * 60 * 60 * 24))
}

export interface ExpiryDescriptor {
  /**
   * Rendered as a `Badge` only when the expiry is news — past, or within 60
   * days. Beyond that the date is a metadata fact, not a status.
   */
  badge: { variant: "destructive" | "secondary"; label: string } | null
  /** The metadata-line fact, present exactly when `badge` is null. */
  fact: string | null
  /** Exact date and days remaining, so the shorter label loses nothing. */
  tooltip: string
}

/**
 * How a credential's expiry should read.
 *
 * Replaces the four hand-rolled colour tiers this file used to carry
 * (`bg-red-100` / `bg-orange-100` / `bg-amber-100` / `bg-muted`) with the two
 * shared badge variants plus a plain fact: a date eight months out is not a
 * status worth a chip, and "Expired" is not the same kind of thing as
 * "expires in 40 days" wearing a different shade of the same chip.
 */
export function describeExpiry(
  expiryDate: string | null | undefined,
): ExpiryDescriptor | null {
  if (!expiryDate) return null

  const expiry = new Date(expiryDate)
  if (Number.isNaN(expiry.getTime())) return null

  const days = daysUntil(expiry)
  const formatted = expiry.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  })

  if (days < 0) {
    return {
      badge: { variant: "destructive", label: "Expired" },
      fact: null,
      tooltip: `Expired on ${formatted}`,
    }
  }

  if (days <= 60) {
    return {
      badge: {
        variant: "secondary",
        label: days === 0 ? "Expires today" : `Expires in ${days} days`,
      },
      fact: null,
      tooltip: `Expires on ${formatted} (in ${days} days)`,
    }
  }

  return {
    badge: null,
    fact: `Expires ${formatted}`,
    tooltip: `Expires on ${formatted} (in ${days} days)`,
  }
}

/** Expiring within 30 days, or already expired — the card's urgency rank. */
export function isExpiryUrgent(expiryDate: string | null | undefined): boolean {
  if (!expiryDate) return false
  const expiry = new Date(expiryDate)
  if (Number.isNaN(expiry.getTime())) return false
  return daysUntil(expiry) <= 30
}

/**
 * Human-readable copy for the test-connection skip reasons. A skip means the
 * connection is valid but model listing isn't applicable for this credential.
 */
const TEST_SKIP_MESSAGES: Record<string, string> = {
  oauth_token_unsupported:
    "Connection valid — model listing isn't supported for OAuth tokens.",
  no_list_endpoint:
    "Connection valid — this provider doesn't expose a model list.",
  no_base_url: "Enter a Base URL to list available models.",
  unsupported_type: "Connection valid — model listing not supported.",
}

/** The inline "Test connection" result, in one sentence. */
export function describeTestResult(result: AICredentialTestResult): string {
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
