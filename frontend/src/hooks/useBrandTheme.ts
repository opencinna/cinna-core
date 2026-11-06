import { useQuery } from "@tanstack/react-query"

import { BrandThemeService } from "@/client"
import {
  type BrandTheme,
  parseBrandTheme,
} from "@/components/BrandTheme/brandTheme"

export const BRAND_THEME_QUERY_KEY = ["brandTheme"] as const

// A theme changes when an admin publishes one; a sign-in page does not need to
// notice within the minute.
const BRAND_THEME_STALE_TIME = 30 * 60 * 1000

// The consent shell holds its first paint for the theme; a stalled request
// may delay it this long, never block it.
const BRAND_THEME_TIMEOUT_MS = 3000

/**
 * A real answer (a theme, or 204 → null) is cached for the stale time. A
 * timeout or an error **throws**, so it is never cached as "no theme": the
 * page shows stock meanwhile and the next mount or focus asks again, instead
 * of a slow first request leaving the sign-in pages unbranded for 30 minutes.
 */
async function fetchBrandTheme(): Promise<BrandTheme | null> {
  const request = BrandThemeService.getBrandTheme()
  let timer: ReturnType<typeof setTimeout> | undefined
  const timedOut = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => {
      request.cancel()
      reject(new Error("brand theme request timed out"))
    }, BRAND_THEME_TIMEOUT_MS)
  })
  try {
    // 204 (no theme) resolves with no body → null.
    const payload = await Promise.race([request, timedOut])
    return payload ? parseBrandTheme(payload) : null
  } finally {
    clearTimeout(timer)
  }
}

/**
 * The server's published brand theme, parsed, or `null` for stock Cinna.
 *
 * `isPending` is true only while an enabled query has not settled; callers
 * that must not reflow when the theme arrives (the consent shell) wait on it,
 * the rest render stock meanwhile.
 */
export function useBrandTheme({ enabled = true }: { enabled?: boolean } = {}) {
  const query = useQuery({
    queryKey: BRAND_THEME_QUERY_KEY,
    queryFn: fetchBrandTheme,
    staleTime: BRAND_THEME_STALE_TIME,
    retry: false,
    enabled,
  })
  // Branding must never break a sign-in page: an error is stock Cinna.
  return {
    theme: enabled && !query.isError ? (query.data ?? null) : null,
    isPending: enabled && query.isPending,
  }
}
