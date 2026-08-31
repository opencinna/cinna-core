import type { ReactNode } from "react"
import { useEffect, useRef, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Loader2, Users, X } from "lucide-react"

import { UsersService } from "@/client"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "@/components/ui/popover"

interface UserItem {
  id: string
  email: string
  full_name: string | null
}

export interface UserAllowlistSelectedItem {
  // Caller-defined unique key (e.g. grant.id, assignment.id) — passed back via
  // ``onRemove`` so the caller can hit the right delete endpoint.
  id: string
  // The underlying ``user.id`` — used to filter selected users out of the
  // search results.
  userId: string
  // Display label for the pill. Required for a good label since the component
  // no longer loads the full user list — pass the user's name or email.
  // Falls back to the raw id when omitted.
  fallbackLabel?: string
}

interface UserAllowlistPickerProps {
  selected: UserAllowlistSelectedItem[]
  onAdd: (user: UserItem) => void
  onRemove: (item: UserAllowlistSelectedItem) => void
  isAdding?: boolean
  isRemoving?: boolean
  searchPlaceholder?: string
  emptyHint?: string
  // Override the default "Shared with" header (icon + label). Pass ``null``
  // to hide the header entirely.
  label?: ReactNode | null
  // Gate the user-search fetch (e.g. only fetch when picker is visible).
  enabled?: boolean
  // Extra user ids to filter out of search results WITHOUT rendering pills for
  // them (unlike ``selected``, which both filters and renders). Use for users
  // that are already committed elsewhere and shouldn't be re-picked.
  excludeUserIds?: string[]
  // Include the current user in search results. Off by default (sharing with
  // yourself is meaningless for share/assignment pickers). Turn on for pickers
  // where self-selection is valid — e.g. assigning yourself agent-api scopes.
  includeSelf?: boolean
}

const MIN_QUERY_LENGTH = 2

export function UserAllowlistPicker({
  selected,
  onAdd,
  onRemove,
  isAdding,
  isRemoving,
  searchPlaceholder = "Search users to add...",
  emptyHint,
  label,
  enabled = true,
  excludeUserIds,
  includeSelf = false,
}: UserAllowlistPickerProps) {
  const [query, setQuery] = useState("")
  const trimmedQuery = query.trim()
  const inputRef = useRef<HTMLInputElement>(null)
  // Escape / click-outside dismisses the results popover without clearing what
  // was typed; any further typing brings it back.
  const [dismissed, setDismissed] = useState(false)

  // Debounce the query so we don't fire a request on every keystroke once
  // past the minimum length; the search only runs ~250ms after typing stops.
  const [debouncedQuery, setDebouncedQuery] = useState(trimmedQuery)
  useEffect(() => {
    const handle = setTimeout(() => setDebouncedQuery(trimmedQuery), 250)
    return () => clearTimeout(handle)
  }, [trimmedQuery])

  // Server-side search via GET /users/search — available to any authenticated
  // user (unlike the admin-only GET /users/), so non-admin owners can find
  // recipients. The current user is excluded server-side unless ``includeSelf``.
  const {
    data: searchData,
    isFetching: isSearching,
    isError: isSearchError,
  } = useQuery({
    queryKey: ["user-search", debouncedQuery, includeSelf],
    queryFn: () =>
      UsersService.searchUsers({
        q: debouncedQuery,
        limit: 10,
        includeSelf,
      }),
    enabled: enabled && debouncedQuery.length >= MIN_QUERY_LENGTH,
    staleTime: 30000,
  })

  const filteredUserIds = new Set([
    ...selected.map((s) => s.userId),
    ...(excludeUserIds ?? []),
  ])
  const results: UserItem[] = (searchData?.data ?? [])
    .map((u) => ({ id: u.id, email: u.email, full_name: u.full_name ?? null }))
    .filter((u) => !filteredUserIds.has(u.id))

  // While the debounce hasn't caught up to what's typed (or the request is
  // in flight / hasn't produced data yet) we're still "searching" — without
  // this, the gap between keystroke and fetch flashes "No matching users".
  const isOpen = trimmedQuery.length >= MIN_QUERY_LENGTH && !dismissed

  const isDebouncePending = trimmedQuery !== debouncedQuery
  const isLoading =
    isDebouncePending ||
    isSearching ||
    // `searchData` also stays undefined after a failure, so without the error
    // term this "still loading" heuristic would spin forever and the error
    // branch below would be unreachable.
    (debouncedQuery.length >= MIN_QUERY_LENGTH &&
      searchData === undefined &&
      !isSearchError)

  const headerNode =
    label === null
      ? null
      : label ?? (
          <Label className="flex items-center gap-2 text-xs text-muted-foreground">
            <Users className="h-3.5 w-3.5" />
            Shared with
          </Label>
        )

  return (
    <div className="space-y-2">
      {headerNode}
      {selected.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {selected.map((item) => (
            <span
              key={item.id}
              className="flex items-center gap-1 bg-secondary text-secondary-foreground text-xs px-2 py-1 rounded-full"
            >
              {item.fallbackLabel || "Unknown user"}
              <button
                type="button"
                onClick={() => onRemove(item)}
                className="hover:text-destructive transition-colors"
                disabled={isRemoving}
                aria-label="Remove user"
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
        </div>
      )}
      {/* The results render in a portal anchored to the input (Radix Popover),
          NOT as a child of the picker — so a host that clips or scrolls (a
          dialog body with ``overflow-y-auto``, a card with ``overflow-hidden``)
          can neither cut the list off nor gain an inner scrollbar because of
          it. Collision detection flips the list above the input when there is
          no room below. */}
      <Popover
        open={isOpen}
        onOpenChange={(next) => {
          if (!next) setDismissed(true)
        }}
      >
        <PopoverAnchor asChild>
          <Input
            ref={inputRef}
            placeholder={searchPlaceholder}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value)
              setDismissed(false)
            }}
            disabled={isAdding}
          />
        </PopoverAnchor>
        <PopoverContent
          align="start"
          sideOffset={4}
          className="w-[var(--radix-popover-trigger-width)] p-0 overflow-hidden"
          // Keep the caret in the search input: the popover must never take
          // focus when it opens, nor yank it back when it closes.
          onOpenAutoFocus={(e) => e.preventDefault()}
          onCloseAutoFocus={(e) => e.preventDefault()}
          // Clicking (or tabbing back into) the input counts as "outside" for
          // Radix since the input is the anchor, not the trigger — without this
          // the list would close the moment the user clicks their own query.
          onInteractOutside={(e) => {
            const target = e.detail.originalEvent.target
            if (target instanceof Node && inputRef.current?.contains(target)) {
              e.preventDefault()
            }
          }}
        >
          {isLoading ? (
            <div className="flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Searching...
            </div>
          ) : isSearchError ? (
            /* Without this branch a failed search renders as "No matching
               users." — the picker would report a fact about the directory
               when what actually happened is that the request failed, and the
               user would conclude the person they are looking for does not
               exist. */
            <p className="px-3 py-2 text-xs text-destructive" role="alert">
              Couldn't search users. This is a failed request, not an empty
              directory — try again.
            </p>
          ) : results.length > 0 ? (
            <div className="divide-y max-h-60 overflow-y-auto">
              {results.map((u) => (
                <button
                  key={u.id}
                  type="button"
                  className="w-full flex items-center gap-2 px-3 py-2 text-sm text-left hover:bg-accent transition-colors"
                  onClick={() => {
                    onAdd(u)
                    setQuery("")
                  }}
                  disabled={isAdding}
                >
                  <span className="font-medium">{u.full_name || u.email}</span>
                  {u.full_name && (
                    <span className="text-muted-foreground text-xs">{u.email}</span>
                  )}
                </button>
              ))}
            </div>
          ) : (
            <p className="px-3 py-2 text-xs text-muted-foreground">
              No matching users.
            </p>
          )}
        </PopoverContent>
      </Popover>
      {selected.length === 0 && !trimmedQuery && emptyHint && (
        <p className="text-xs text-muted-foreground">{emptyHint}</p>
      )}
    </div>
  )
}
