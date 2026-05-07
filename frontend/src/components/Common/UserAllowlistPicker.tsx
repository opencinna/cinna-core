import type { ReactNode } from "react"
import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Users, X } from "lucide-react"

import { UsersService } from "@/client"
import useAuth from "@/hooks/useAuth"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

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
  // search results and to look up display info from the live users list.
  userId: string
  // Fallback label when the user isn't in the loaded users list (e.g. soft-
  // deleted user). The component prefers ``full_name``/``email`` from the
  // users query when available.
  fallbackLabel?: string
}

interface UserAllowlistPickerProps {
  selected: UserAllowlistSelectedItem[]
  onAdd: (user: UserItem) => void
  onRemove: (item: UserAllowlistSelectedItem) => void
  isAdding?: boolean
  isRemoving?: boolean
  excludeSelf?: boolean
  searchPlaceholder?: string
  emptyHint?: string
  // Override the default "Shared with" header (icon + label). Pass ``null``
  // to hide the header entirely.
  label?: ReactNode | null
  // Gate the users-list fetch (e.g. only fetch when picker is visible).
  enabled?: boolean
}

export function UserAllowlistPicker({
  selected,
  onAdd,
  onRemove,
  isAdding,
  isRemoving,
  excludeSelf = true,
  searchPlaceholder = "Search users to add...",
  emptyHint,
  label,
  enabled = true,
}: UserAllowlistPickerProps) {
  const { user: currentUser } = useAuth()
  const [query, setQuery] = useState("")

  const { data: usersData } = useQuery({
    queryKey: ["users-list"],
    queryFn: () => UsersService.readUsers({ limit: 200 }),
    enabled,
    staleTime: 30000,
  })
  const allUsers: UserItem[] =
    ((usersData as { data?: UserItem[] })?.data ?? [])

  const selectedUserIds = selected.map((s) => s.userId)
  const filteredUsers = allUsers.filter(
    (u) =>
      (!excludeSelf || u.id !== currentUser?.id) &&
      !selectedUserIds.includes(u.id) &&
      (u.email.toLowerCase().includes(query.toLowerCase()) ||
        (u.full_name ?? "").toLowerCase().includes(query.toLowerCase())),
  )

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
          {selected.map((item) => {
            const u = allUsers.find((usr) => usr.id === item.userId)
            const display =
              u?.full_name || u?.email || item.fallbackLabel || item.userId
            return (
              <span
                key={item.id}
                className="flex items-center gap-1 bg-secondary text-secondary-foreground text-xs px-2 py-1 rounded-full"
              >
                {display}
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
            )
          })}
        </div>
      )}
      <Input
        placeholder={searchPlaceholder}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        disabled={isAdding}
      />
      {query && filteredUsers.length > 0 && (
        <div className="border rounded-md divide-y max-h-36 overflow-y-auto">
          {filteredUsers.slice(0, 8).map((u) => (
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
      )}
      {query && filteredUsers.length === 0 && (
        <p className="text-xs text-muted-foreground">No matching users.</p>
      )}
      {selected.length === 0 && !query && emptyHint && (
        <p className="text-xs text-muted-foreground">{emptyHint}</p>
      )}
    </div>
  )
}
