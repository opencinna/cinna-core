import { useQuery } from "@tanstack/react-query"
import { Globe, Loader2, Plus, User, Users } from "lucide-react"
import { type ReactNode, useEffect, useState } from "react"

import { UsersService } from "@/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import type { LibraryPrincipal } from "@/utils/libraryAccess"
import { USER_ROLE_OPTIONS } from "@/utils/userRoles"

const MIN_QUERY_LENGTH = 2

interface LibraryPrincipalPickerProps {
  /** `principalKey`s already on the list; they are not offered again. */
  grantedKeys: Set<string>
  onPick: (principal: LibraryPrincipal, title: string) => void
  disabled?: boolean
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
 * "Add people or roles": one trigger over a `Popover` holding Everyone, the
 * three roles and a people search (§2 Choosing one entity in a form; search
 * skeleton from `Common/UserSelectField`). Each item is an *add* action, not
 * a selected value, so the items are buttons rather than radios. Whatever is
 * already granted is left out. Never a nested `Dialog`.
 */
export function LibraryPrincipalPicker({
  grantedKeys,
  onPick,
  disabled = false,
}: LibraryPrincipalPickerProps) {
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

  const showEveryone =
    !grantedKeys.has("everyone") && "everyone".includes(lowered)
  const roles = USER_ROLE_OPTIONS.filter(
    (r) =>
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

  const pick = (principal: LibraryPrincipal, title: string) => {
    onPick(principal, title)
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
        icon={<User className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
        label={u.full_name || u.email}
        detail={u.full_name ? u.email : null}
        onClick={() => pick({ user_id: u.id }, u.full_name || u.email)}
      />
    ))
  }

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
          Add people or roles
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 p-2">
        <Input
          autoFocus
          placeholder="Search people or roles..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search people or roles"
        />
        <div className="mt-1 max-h-[40vh] overflow-y-auto pr-2">
          {showEveryone && (
            <>
              <GroupHeading>Everyone</GroupHeading>
              <PickItem
                icon={
                  <Globe className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                }
                label="Everyone"
                onClick={() => pick("everyone", "Everyone")}
              />
            </>
          )}
          {roles.length > 0 && (
            <>
              <GroupHeading>Roles</GroupHeading>
              {roles.map((r) => (
                <PickItem
                  key={r.value}
                  icon={
                    <Users className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  }
                  label={r.label}
                  onClick={() => pick({ role: r.value }, r.label)}
                />
              ))}
            </>
          )}
          <GroupHeading>People</GroupHeading>
          {peopleBody}
        </div>
      </PopoverContent>
    </Popover>
  )
}
