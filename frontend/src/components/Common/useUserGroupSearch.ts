import { useQuery } from "@tanstack/react-query"
import { useEffect, useState } from "react"

import { type UserGroupSearchResult, UserGroupsService } from "@/client"

export const USER_GROUP_SEARCH_LIMIT = 10

/**
 * The one user-group search every share picker uses (`UserAllowlistPicker`'s
 * group mode, `CatalogPrincipalPicker`). Backed by the non-admin
 * `GET /user-groups/search` (D2: open to any signed-in user, no role gate),
 * debounced 250 ms like the people search. An empty query returns the first
 * groups by name, so a picker can offer some before anything is typed.
 */
export function useUserGroupSearch(
  query: string,
  {
    enabled = true,
    minLength = 0,
  }: { enabled?: boolean; minLength?: number } = {},
): {
  groups: UserGroupSearchResult[]
  isSearching: boolean
  isError: boolean
} {
  const trimmed = query.trim()
  const [debounced, setDebounced] = useState(trimmed)
  useEffect(() => {
    const handle = setTimeout(() => setDebounced(trimmed), 250)
    return () => clearTimeout(handle)
  }, [trimmed])

  const { data, isFetching, isError } = useQuery({
    queryKey: ["user-group-search", debounced],
    queryFn: () =>
      UserGroupsService.searchUserGroups({
        q: debounced || undefined,
        limit: USER_GROUP_SEARCH_LIMIT,
      }),
    enabled: enabled && debounced.length >= minLength,
    staleTime: 30000,
  })

  return {
    groups: data?.data ?? [],
    // Debounce-pending counts as searching too, so the gap between a
    // keystroke and the fetch never flashes "No matching groups."
    isSearching:
      trimmed !== debounced ||
      isFetching ||
      (enabled &&
        debounced.length >= minLength &&
        data === undefined &&
        !isError),
    isError,
  }
}
