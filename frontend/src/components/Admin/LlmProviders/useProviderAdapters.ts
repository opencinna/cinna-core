import { useQuery } from "@tanstack/react-query"

import { AdminAiProvidersService, type ProviderAdapterPublic } from "@/client"
import type { AICredentialType } from "@/client"
import { getProviderTypeLabel } from "./providerTypes"

/**
 * What the server knows about each AI provider **type** (the vendor).
 *
 * `GET /admin/ai-providers/adapters` publishes one row per registered adapter,
 * and it is the only place any of these facts are stated — which vendors exist,
 * what they are called, which of them can create keys through an administration
 * API, and what an administration secret for one needs. The browser keeps no
 * copy of any of that: a sixth vendor is a module on the server, and this hook
 * picks it up with no frontend edit.
 *
 * Superuser-only, like the endpoint. Every caller today is an admin surface.
 */
/**
 * A **sibling** of `AI_PROVIDERS_QUERY_KEY`, not a child of it.
 *
 * `invalidateQueries` is prefix-matching, so `["admin","ai-providers","adapters"]`
 * would be invalidated by every provider mutation — pressing Verify on one row
 * would refetch the adapter registry, defeating the five-minute `staleTime`
 * below whose whole premise is that the registry changes only on deploy.
 */
export const PROVIDER_ADAPTERS_QUERY_KEY = [
  "admin",
  "ai-provider-adapters",
] as const

export interface UseProviderAdaptersResult {
  adapters: ProviderAdapterPublic[]
  /** The adapter for a type, or `undefined` while the list is still loading. */
  adapterFor: (type: AICredentialType) => ProviderAdapterPublic | undefined
  /**
   * The vendor's display label.
   *
   * Falls back to the shared label map — never to the wire value. The window
   * before this query answers, and the case where it errors outright, are both
   * real: a fallback of `type` renders `minimax` to an administrator.
   */
  typeLabel: (type: AICredentialType) => string
  /**
   * Whether per-user keys can be minted for this type.
   *
   * This is the **whole** of the create wizard's precondition. A provider
   * carries its own administration secret, so there is nothing to connect
   * first; the server says the same thing in one term
   * (`AIProvidersService._validate_shape`). The retired second term,
   * `can_mint_now`, additionally required a provider of that type to exist
   * already, which would have made the first OpenAI provider an admin ever
   * connects unable to be minted.
   *
   * `false` while the list is loading, so a control gated on it stays off
   * until the server has actually said yes. Loading is not a yes.
   */
  supportsMinting: (type: AICredentialType) => boolean
  isPending: boolean
  isError: boolean
}

export function useProviderAdapters(enabled = true): UseProviderAdaptersResult {
  const { data, isPending, isError } = useQuery({
    queryKey: PROVIDER_ADAPTERS_QUERY_KEY,
    queryFn: () => AdminAiProvidersService.listProviderAdapters(),
    // The registry only changes on deploy.
    staleTime: 5 * 60_000,
    enabled,
  })

  const adapters = data?.data ?? []
  const adapterFor = (type: AICredentialType) =>
    adapters.find((adapter) => adapter.type === type)

  return {
    adapters,
    adapterFor,
    typeLabel: (type: AICredentialType) =>
      adapterFor(type)?.label ?? getProviderTypeLabel(type),
    supportsMinting: (type: AICredentialType) =>
      adapterFor(type)?.supports_minting === true,
    isPending: enabled && isPending,
    isError,
  }
}

/**
 * One field of an adapter's `admin_config_schema`.
 *
 * The schema is the adapter's own description of what connecting that provider
 * organisation requires, rendered as given. Nothing here is a per-provider
 * table in the browser: an adapter that asks for a fourth field gets a fourth
 * input with no frontend change.
 */
export interface AdminConfigField {
  name: string
  label: string
  required: boolean
  help?: string
  /** `"integer"` renders a number input and submits a number. */
  type?: string
}

/** The declared fields, or `[]` for an adapter that cannot mint. */
export function adminConfigFields(
  adapter: ProviderAdapterPublic | undefined,
): AdminConfigField[] {
  const raw = (adapter?.admin_config_schema as { fields?: unknown } | null)?.fields
  if (!Array.isArray(raw)) return []
  return raw.flatMap((entry) => {
    if (!entry || typeof entry !== "object") return []
    const field = entry as Record<string, unknown>
    if (typeof field.name !== "string" || typeof field.label !== "string") return []
    return [
      {
        name: field.name,
        label: field.label,
        required: field.required === true,
        help: typeof field.help === "string" ? field.help : undefined,
        type: typeof field.type === "string" ? field.type : undefined,
      },
    ]
  })
}
