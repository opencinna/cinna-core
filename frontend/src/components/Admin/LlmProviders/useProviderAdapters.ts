import { useQuery } from "@tanstack/react-query"

import { AdminProviderAdaptersService, type ProviderAdapterPublic } from "@/client"
import type { AICredentialType } from "@/client"

/**
 * What the server knows about each AI provider.
 *
 * `GET /admin/provider-adapters` publishes one row per registered adapter, and
 * it is the only place any of these facts are stated — which providers exist,
 * what they are called, which of them can create keys through an administration
 * API, and what an administration credential for one needs. The browser keeps
 * no copy of any of that: a sixth provider is a module on the server, and this
 * hook picks it up with no frontend edit.
 *
 * Superuser-only, like the endpoint. Every caller today is an admin surface.
 */
export const PROVIDER_ADAPTERS_QUERY_KEY = ["admin", "provider-adapters"] as const

export interface UseProviderAdaptersResult {
  adapters: ProviderAdapterPublic[]
  /** Adapters whose administration API can create keys. */
  mintingAdapters: ProviderAdapterPublic[]
  /** The adapter for a type, or `undefined` while the list is still loading. */
  adapterFor: (type: AICredentialType) => ProviderAdapterPublic | undefined
  /**
   * Whether per-user keys can be minted for this provider.
   *
   * `false` while the list is loading, so a control gated on it stays off
   * until the server has actually said yes. Loading is not a yes.
   */
  supportsMinting: (type: AICredentialType) => boolean
  /**
   * Whether an administrator may choose per-user minting for this provider
   * right now — the server's own answer, not a conjunction assembled here.
   *
   * `false` while the list is loading, for the same reason as above.
   */
  canMintNow: (type: AICredentialType) => boolean
  isPending: boolean
  isError: boolean
}

export function useProviderAdapters(enabled = true): UseProviderAdaptersResult {
  const { data, isPending, isError } = useQuery({
    queryKey: PROVIDER_ADAPTERS_QUERY_KEY,
    queryFn: () => AdminProviderAdaptersService.listProviderAdapters(),
    // The registry only changes on deploy.
    staleTime: 5 * 60_000,
    enabled,
  })

  const adapters = data?.data ?? []
  const adapterFor = (type: AICredentialType) =>
    adapters.find((adapter) => adapter.type === type)

  return {
    adapters,
    mintingAdapters: adapters.filter((adapter) => adapter.supports_minting),
    adapterFor,
    supportsMinting: (type: AICredentialType) =>
      adapterFor(type)?.supports_minting === true,
    canMintNow: (type: AICredentialType) =>
      adapterFor(type)?.can_mint_now === true,
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
