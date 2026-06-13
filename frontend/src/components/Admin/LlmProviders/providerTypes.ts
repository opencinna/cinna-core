import type { AICredentialType } from "@/client"

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
export const MANAGED_CREDENTIALS_QUERY_PREFIX = ["admin", "llm-providers"] as const

// React Query key for the fleet-wide managed-credential list, optionally
// scoped to a single target user.
export function managedCredentialsQueryKey(targetUserId?: string | null) {
  return [...MANAGED_CREDENTIALS_QUERY_PREFIX, targetUserId ?? "all"] as const
}
