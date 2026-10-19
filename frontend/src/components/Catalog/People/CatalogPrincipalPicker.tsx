import { useQuery } from "@tanstack/react-query"
import { Loader2, Plus } from "lucide-react"
import { type ReactNode, useEffect, useState } from "react"

import { UsersService } from "@/client"
import { useUserGroupSearch } from "@/components/Common/useUserGroupSearch"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import type { CatalogPrincipal } from "@/utils/catalogAccess"
import { pluralize } from "@/utils/pluralize"
import { PRINCIPAL_KIND_ICON } from "@/utils/principalKinds"
import { USER_ROLE_OPTIONS } from "@/utils/userRoles"

const MIN_QUERY_LENGTH = 2
const MAX_GROUP_RESULTS = 5

const RoleIcon = PRINCIPAL_KIND_ICON.role
const GroupIcon = PRINCIPAL_KIND_ICON.group
const PersonIcon = PRINCIPAL_KIND_ICON.user

/** What a picked principal carries besides its title. */
export interface PickedPrincipalDetail {
  /** A person's email, so a list can tell two people apart. */
  email?: string
  /** A group's member count, for the row's detail flag. */
  memberCount?: number
}

function pickerNouns(allowRoles: boolean, allowGroups: boolean) {
  if (allowRoles && allowGroups) return "people, groups or roles"
  if (allowGroups) return "people or groups"
  if (allowRoles) return "people or roles"
  return "people"
}

interface CatalogPrincipalPickerProps {
  /** `principalKey`s already on the list; they are not offered again. */
  grantedKeys: Set<string>
  onPick: (
    principal: CatalogPrincipal,
    title: string,
    detail: PickedPrincipalDetail,
  ) => void
  disabled?: boolean
  /**
   * Offer the role group. Off for surfaces whose grants cannot name a role
   * (artifacts, D21: a role principal is a 422) — the copy then drops "roles".
   */
  allowRoles?: boolean
  /** Offer user groups (D1: every catalog family and artifacts take them). */
  allowGroups?: boolean
}

function GroupHeading({ children }: { children: ReactNode }) {
  return (
    <p className="px-2 pt-2 pb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
      {children}
    </p>
  )
}

function PickItem({
  icon,
  label,
  detail,
  onClick,
}: {
  icon: ReactNode
  label: string
  detail?: string | null
  onClick: () => void
}) {
  return (
    <button
      type="button"
      className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm transition-colors hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
      onClick={onClick}
    >
      {icon}
      <span className="truncate">{label}</span>
      {detail && (
        <span className="truncate text-xs text-muted-foreground">{detail}</span>
      )}
    </button>
  )
}

/**
 * "Add people, groups or roles": one trigger over a `Popover` holding the
 * three roles, a group search and a people search (§2 Choosing one entity in a form; search skeleton from
 * `Common/UserSelectField`). Each item is an *add* action, not a selected
 * value, so the items are buttons rather than radios. Whatever is already
 * granted is left out (§2 The same entity). No Everyone: that is General
 * access. Never a nested `Dialog`. Rebuilt from the team library's picker.
 */
export function CatalogPrincipalPicker({
  grantedKeys,
  onPick,
  disabled = false,
  allowRoles = true,
  allowGroups = true,
}: CatalogPrincipalPickerProps) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState("")
  const trimmed = query.trim()
  const lowered = trimmed.toLowerCase()

  const [debounced, setDebounced] = useState(trimmed)
  useEffect(() => {
    const handle = setTimeout(() => setDebounced(trimmed), 250)
    return () => clearTimeout(handle)
  }, [trimmed])

  const { data, isFetching, isError } = useQuery({
    queryKey: ["user-search", debounced, true],
    queryFn: () =>
      UsersService.searchUsers({ q: debounced, limit: 10, includeSelf: true }),
    enabled: open && debounced.length >= MIN_QUERY_LENGTH,
    staleTime: 30000,
  })

  // Groups need no minimum: an empty query lists the first ones by name.
  const groupSearch = useUserGroupSearch(trimmed, {
    enabled: open && allowGroups,
  })
  const groups = groupSearch.groups
    .filter((g) => !grantedKeys.has(`group:${g.id}`))
    .slice(0, MAX_GROUP_RESULTS)

  const roles = USER_ROLE_OPTIONS.filter(
    (r) =>
      allowRoles &&
      !grantedKeys.has(`role:${r.value}`) &&
      r.label.toLowerCase().includes(lowered),
  )
  const people = (data?.data ?? []).filter(
    (u) => !grantedKeys.has(`user:${u.id}`),
  )
  const searching =
    trimmed !== debounced ||
    isFetching ||
    (debounced.length >= MIN_QUERY_LENGTH && data === undefined && !isError)

  const pick = (
    principal: CatalogPrincipal,
    title: string,
    detail: PickedPrincipalDetail = {},
  ) => {
    onPick(principal, title, detail)
    setOpen(false)
    setQuery("")
  }

  let peopleBody: ReactNode
  if (trimmed.length < MIN_QUERY_LENGTH) {
    peopleBody = (
      <p className="px-2 py-1 text-xs text-muted-foreground">
        Type at least {MIN_QUERY_LENGTH} characters to find people.
      </p>
    )
  } else if (searching) {
    peopleBody = (
      <div className="flex items-center gap-2 px-2 py-1 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Searching…
      </div>
    )
  } else if (isError) {
    peopleBody = (
      <p className="px-2 py-1 text-xs text-destructive" role="alert">
        Couldn't search people. Try again.
      </p>
    )
  } else if (people.length === 0) {
    peopleBody = (
      <p className="px-2 py-1 text-xs text-muted-foreground">
        No matching people.
      </p>
    )
  } else {
    peopleBody = people.map((u) => (
      <PickItem
        key={u.id}
        icon={
          <PersonIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        }
        label={u.full_name || u.email}
        detail={u.full_name ? u.email : null}
        onClick={() =>
          pick({ user_id: u.id }, u.full_name || u.email, { email: u.email })
        }
      />
    ))
  }

  let groupBody: ReactNode
  if (groupSearch.isSearching) {
    groupBody = (
      <div className="flex items-center gap-2 px-2 py-1 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Searching…
      </div>
    )
  } else if (groupSearch.isError) {
    groupBody = (
      <p className="px-2 py-1 text-xs text-destructive" role="alert">
        Couldn't search groups. Try again.
      </p>
    )
  } else if (groups.length === 0) {
    groupBody = (
      <p className="px-2 py-1 text-xs text-muted-foreground">
        No matching groups.
      </p>
    )
  } else {
    groupBody = groups.map((g) => (
      <PickItem
        key={g.id}
        icon={
          <GroupIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        }
        label={g.name}
        detail={pluralize(g.member_count ?? 0, "member", "members")}
        onClick={() =>
          pick({ group_id: g.id }, g.name, { memberCount: g.member_count ?? 0 })
        }
      />
    ))
  }

  const nouns = pickerNouns(allowRoles, allowGroups)

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (!next) setQuery("")
      }}
    >
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled}
          data-add-principal
        >
          <Plus className="mr-1 h-4 w-4" />
          Add {nouns}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 p-2">
        <Input
          autoFocus
          placeholder={`Search ${nouns}...`}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label={`Search ${nouns}`}
        />
        <div className="mt-1 max-h-[40vh] overflow-y-auto pr-2">
          {roles.length > 0 && (
            <>
              <GroupHeading>Roles</GroupHeading>
              {roles.map((r) => (
                <PickItem
                  key={r.value}
                  icon={
                    <RoleIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  }
                  label={r.label}
                  onClick={() => pick({ role: r.value }, r.label)}
                />
              ))}
            </>
          )}
          {allowGroups && (
            <>
              <GroupHeading>Groups</GroupHeading>
              {groupBody}
            </>
          )}
          {(allowRoles || allowGroups) && <GroupHeading>People</GroupHeading>}
          {peopleBody}
        </div>
      </PopoverContent>
    </Popover>
  )
}
