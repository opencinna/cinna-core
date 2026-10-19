import { useQuery } from "@tanstack/react-query"
import { Loader2, Share2, Users, X } from "lucide-react"
import type { ReactNode } from "react"
import { useEffect, useRef, useState } from "react"

import { type UserGroupSearchResult, UsersService } from "@/client"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { pluralize } from "@/utils/pluralize"
import { useUserGroupSearch } from "./useUserGroupSearch"

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

export interface GroupSelectedItem {
  // Caller-defined unique key, passed back via ``groups.onRemove``.
  id: string
  // The user group's id, used to filter it out of the group results.
  groupId: string
  name: string
  memberCount?: number | null
}

/** Opt-in group mode: a "Groups" section above "People" in one search. */
export interface UserAllowlistGroupsMode {
  selected: GroupSelectedItem[]
  onAdd: (group: UserGroupSearchResult) => void
  onRemove: (item: GroupSelectedItem) => void
  isAdding?: boolean
  isRemoving?: boolean
  // Group ids already committed elsewhere: filtered out of the results
  // without a pill (the group twin of ``excludeUserIds``).
  excludeGroupIds?: string[]
}

interface UserAllowlistPickerProps {
  selected?: UserAllowlistSelectedItem[]
  onAdd?: (user: UserItem) => void
  onRemove?: (item: UserAllowlistSelectedItem) => void
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
  // Offer user groups too (shown first, ≤ 5 results). Without it the picker
  // is people only, exactly as before.
  groups?: UserAllowlistGroupsMode
  // ``false`` gives a groups-only picker (requires ``groups``).
  people?: boolean
}

const MIN_QUERY_LENGTH = 2
const MAX_GROUP_RESULTS = 5

function SectionHeading({ children }: { children: ReactNode }) {
  return (
    <p className="px-3 pt-2 pb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
      {children}
    </p>
  )
}

function SectionStatus({
  state,
  noun,
}: {
  state: "searching" | "error" | "empty"
  noun: "users" | "people" | "groups"
}) {
  if (state === "searching") {
    return (
      <div className="flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Searching...
      </div>
    )
  }
  if (state === "error") {
    /* Without this branch a failed search renders as "No matching …" — the
       picker would report a fact about the directory when what actually
       happened is that the request failed. */
    return (
      <p className="px-3 py-2 text-xs text-destructive" role="alert">
        Couldn't search {noun}. This is a failed request, not an empty directory
        — try again.
      </p>
    )
  }
  return (
    <p className="px-3 py-2 text-xs text-muted-foreground">
      No matching {noun}.
    </p>
  )
}

function RemovePillButton({
  label,
  disabled,
  onClick,
}: {
  label: string
  disabled?: boolean
  onClick: () => void
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onClick}
          className="hover:text-destructive transition-colors"
          disabled={disabled}
          aria-label={label}
        >
          <X className="h-3 w-3" />
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        Remove
      </TooltipContent>
    </Tooltip>
  )
}

export function UserAllowlistPicker({
  selected = [],
  onAdd,
  onRemove,
  isAdding: isAddingPerson,
  isRemoving,
  searchPlaceholder = "Search users to add...",
  emptyHint,
  label,
  enabled = true,
  excludeUserIds,
  includeSelf = false,
  groups,
  people = true,
}: UserAllowlistPickerProps) {
  const isAdding = isAddingPerson || groups?.isAdding
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
    enabled: people && enabled && debouncedQuery.length >= MIN_QUERY_LENGTH,
    staleTime: 30000,
  })

  const groupSearch = useUserGroupSearch(trimmedQuery, {
    enabled: !!groups && enabled,
    minLength: MIN_QUERY_LENGTH,
  })
  const selectedGroupIds = new Set([
    ...(groups?.selected ?? []).map((g) => g.groupId),
    ...(groups?.excludeGroupIds ?? []),
  ])
  const groupResults = groupSearch.groups
    .filter((g) => !selectedGroupIds.has(g.id))
    .slice(0, MAX_GROUP_RESULTS)

  const filteredUserIds = new Set([
    ...selected.map((s) => s.userId),
    ...(excludeUserIds ?? []),
  ])
  const results: UserItem[] = (searchData?.data ?? [])
    .map((u) => ({ id: u.id, email: u.email, full_name: u.full_name ?? null }))
    .filter((u) => !filteredUserIds.has(u.id))

  const selectedGroups = groups?.selected ?? []

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

  const peopleState = isLoading
    ? "searching"
    : isSearchError
      ? "error"
      : results.length === 0
        ? "empty"
        : null
  const peopleList = (
    <div
      className={groups ? "divide-y" : "divide-y max-h-60 overflow-y-auto pr-2"}
    >
      {results.map((u) => (
        <button
          key={u.id}
          type="button"
          className="w-full flex items-center gap-2 px-3 py-2 text-sm text-left hover:bg-accent transition-colors"
          onClick={() => {
            onAdd?.(u)
            setQuery("")
            // The result button unmounts with the list; keep focus in the
            // search rather than letting it drop to <body>.
            inputRef.current?.focus()
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
  )
  const peopleBody = peopleState ? (
    <SectionStatus state={peopleState} noun={groups ? "people" : "users"} />
  ) : (
    peopleList
  )

  const groupState = groupSearch.isSearching
    ? "searching"
    : groupSearch.isError
      ? "error"
      : groupResults.length === 0
        ? "empty"
        : null
  const groupBody = groupState ? (
    <SectionStatus state={groupState} noun="groups" />
  ) : (
    <div className="divide-y">
      {groupResults.map((g) => (
        <button
          key={g.id}
          type="button"
          className="w-full flex items-center gap-2 px-3 py-2 text-sm text-left hover:bg-accent transition-colors"
          onClick={() => {
            groups?.onAdd(g)
            setQuery("")
            inputRef.current?.focus()
          }}
          disabled={isAdding}
        >
          <Users className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          <span className="truncate font-medium">{g.name}</span>
          <span className="shrink-0 text-muted-foreground text-xs">
            {pluralize(g.member_count ?? 0, "member", "members")}
          </span>
        </button>
      ))}
    </div>
  )

  const headerNode =
    label === null
      ? null
      : (label ?? (
          <Label className="flex items-center gap-2 text-xs text-muted-foreground">
            <Share2 className="h-3.5 w-3.5" />
            Shared with
          </Label>
        ))

  return (
    <div className="space-y-2">
      {headerNode}
      {(selected.length > 0 || selectedGroups.length > 0) && (
        <div className="flex flex-wrap gap-1.5">
          {selectedGroups.map((item) => (
            <span
              key={`group:${item.id}`}
              className="flex min-w-0 max-w-full items-center gap-1 bg-secondary text-secondary-foreground text-xs px-2 py-1 rounded-full"
            >
              <Users className="h-3 w-3 shrink-0" aria-hidden />
              <span className="min-w-0 truncate">{item.name}</span>
              {item.memberCount != null && (
                <span className="shrink-0 text-muted-foreground">
                  · {pluralize(item.memberCount, "member", "members")}
                </span>
              )}
              <RemovePillButton
                label={`Remove group ${item.name}`}
                disabled={groups?.isRemoving}
                onClick={() => groups?.onRemove(item)}
              />
            </span>
          ))}
          {selected.map((item) => {
            const pillLabel = item.fallbackLabel || "Unknown user"
            return (
              <span
                key={item.id}
                className="flex items-center gap-1 bg-secondary text-secondary-foreground text-xs px-2 py-1 rounded-full"
              >
                {pillLabel}
                <RemovePillButton
                  label={`Remove ${pillLabel}`}
                  disabled={isRemoving}
                  onClick={() => onRemove?.(item)}
                />
              </span>
            )
          })}
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
          {groups ? (
            <div className="max-h-[40vh] overflow-y-auto pr-2">
              <SectionHeading>Groups</SectionHeading>
              {groupBody}
              {people && (
                <>
                  <SectionHeading>People</SectionHeading>
                  {peopleBody}
                </>
              )}
            </div>
          ) : (
            peopleBody
          )}
        </PopoverContent>
      </Popover>
      {selected.length === 0 &&
        selectedGroups.length === 0 &&
        !trimmedQuery &&
        emptyHint && (
          <p className="text-xs text-muted-foreground">{emptyHint}</p>
        )}
    </div>
  )
}
