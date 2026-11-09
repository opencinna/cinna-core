import {
  type QueryClient,
  useIsMutating,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"

import {
  ApiError,
  type BrandThemeDraftPublic,
  type BrandThemeIssue,
  type BrandThemePublishErrors,
  BrandThemeService,
} from "@/client"
import { BRAND_THEME_QUERY_KEY } from "@/hooks/useBrandTheme"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"

/** Every stored theme, newest first; the published one included. */
export const BRAND_THEME_DRAFTS_KEY = ["admin", "brandThemeDrafts"] as const

/**
 * Unpublish is started from the published row in the card and in its Sheet,
 * each with its own confirm; one key lets both show the same wait.
 */
const UNPUBLISH_KEY = ["admin", "brandTheme", "unpublish"] as const

export function useBrandThemeDrafts() {
  return useQuery({
    queryKey: BRAND_THEME_DRAFTS_KEY,
    queryFn: () => BrandThemeService.listBrandThemeDrafts(),
  })
}

/** Every write changes both the admin list and what the sign-in pages show. */
export function invalidateBrandTheme(queryClient: QueryClient) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: BRAND_THEME_DRAFTS_KEY }),
    queryClient.invalidateQueries({ queryKey: BRAND_THEME_QUERY_KEY }),
  ])
}

export function isPublished(draft: BrandThemeDraftPublic): boolean {
  return draft.status === "published"
}

/**
 * The findings of a publish refused with 409 (`{errors: [...]}`, the rules
 * tightened since upload), or null for any other failure.
 */
export function publishRefusal(error: unknown): BrandThemeIssue[] | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const errors = (error.body as Partial<BrandThemePublishErrors> | undefined)
    ?.errors
  return Array.isArray(errors) ? errors : null
}

export function useUnpublishBrandTheme({
  onSuccess,
}: {
  onSuccess?: () => void
} = {}) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  return useMutation({
    mutationKey: UNPUBLISH_KEY,
    mutationFn: () => BrandThemeService.unpublishBrandTheme(),
    onSuccess: () => {
      showSuccessToast("Theme unpublished — this server shows stock Cinna")
      onSuccess?.()
    },
    onError: (error) =>
      showErrorToast(getErrorMessage(error, "The theme was not unpublished.")),
    onSettled: () => invalidateBrandTheme(queryClient),
  })
}

/** True while an unpublish started anywhere on the page is in flight. */
export function useIsUnpublishing(): boolean {
  return useIsMutating({ mutationKey: UNPUBLISH_KEY }) > 0
}
