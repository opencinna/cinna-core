import { useQuery } from "@tanstack/react-query"
import { Check, ChevronDown, Loader2 } from "lucide-react"
import { useEffect, useId, useState } from "react"

import { UsersService } from "@/client"
import { Input } from "@/components/ui/input"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { cn } from "@/lib/utils"

export interface UserSelectValue {
  userId: string
  /** Name, else email — what the trigger shows. */
  label: string
}

interface UserSelectFieldProps {
  value: UserSelectValue | null
  onChange: (value: UserSelectValue) => void
  placeholder?: string
  /** Include the current user in results (self-issue, self-grant). */
  includeSelf?: boolean
  /** Users that may not be picked (already granted). */
  excludeUserIds?: string[]
  disabled?: boolean
  /** Associates an external `<Label htmlFor>` with the trigger. */
  id?: string
}

const MIN_QUERY_LENGTH = 2

/**
 * One person as a **form field** (§2 "Choosing one entity in a form"): a
 * `Select`-shaped trigger carrying the chosen person, over a `Popover` that
 * holds the search and returns one value. The trigger is how it is changed;
 * there is no chip with an `X` and no search box left open under the value.
 *
 * `UserAllowlistPicker` is the multi-select sibling (a set of people, as
 * chips); a one-person field must not use it — a second pick silently
 * replaces the first while the UI reads as "add another".
 *
 * Search is the same server-side `GET /users/search` the allowlist uses.
 */
export function UserSelectField({
  value,
  onChange,
  placeholder = "Pick a person",
  includeSelf = false,
  excludeUserIds,
  disabled = false,
  id,
}: UserSelectFieldProps) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState("")
  const trimmed = query.trim()
  const listId = useId()

  const [debounced, setDebounced] = useState(trimmed)
  useEffect(() => {
    const handle = setTimeout(() => setDebounced(trimmed), 250)
    return () => clearTimeout(handle)
  }, [trimmed])

  const { data, isFetching, isError } = useQuery({
    queryKey: ["user-search", debounced, includeSelf],
    queryFn: () =>
      UsersService.searchUsers({ q: debounced, limit: 10, includeSelf }),
    enabled: open && debounced.length >= MIN_QUERY_LENGTH,
    staleTime: 30000,
  })

  const excluded = new Set(excludeUserIds ?? [])
  const results = (data?.data ?? []).filter((u) => !excluded.has(u.id))
  const isLoading =
    trimmed !== debounced ||
    isFetching ||
    (debounced.length >= MIN_QUERY_LENGTH && data === undefined && !isError)

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (!next) setQuery("")
      }}
    >
      <PopoverTrigger asChild>
        <button
          id={id}
          type="button"
          disabled={disabled}
          className={cn(
            "flex min-h-9 w-full items-center justify-between gap-2 rounded-md border border-input",
            "bg-transparent px-3 py-1.5 text-left text-sm shadow-xs outline-none transition-[color,box-shadow]",
            "focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]",
            "disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30 dark:hover:bg-input/50",
          )}
        >
          <span
            className={cn("truncate", !value && "text-muted-foreground")}
          >
            {value ? value.label : placeholder}
          </span>
          <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-(--radix-popover-trigger-width) p-2"
      >
        <Input
          autoFocus
          placeholder="Search by name or email..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-controls={listId}
        />
        <div id={listId} className="mt-2">
          {trimmed.length < MIN_QUERY_LENGTH ? (
            <p className="px-1 py-1 text-xs text-muted-foreground">
              Type at least {MIN_QUERY_LENGTH} characters.
            </p>
          ) : isLoading ? (
            <div className="flex items-center gap-2 px-1 py-1 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Searching...
            </div>
          ) : isError ? (
            <p className="px-1 py-1 text-xs text-destructive" role="alert">
              Couldn't search users. Try again.
            </p>
          ) : results.length === 0 ? (
            <p className="px-1 py-1 text-xs text-muted-foreground">
              No matching users.
            </p>
          ) : (
            <div
              role="radiogroup"
              aria-label="People"
              className="max-h-60 divide-y overflow-y-auto pr-2"
            >
              {results.map((u) => {
                const selected = value?.userId === u.id
                return (
                  // biome-ignore lint/a11y/useSemanticElements: a result row carries a name and an email line; a native radio input cannot, and §2 Choice-control ARIA asks for role="radio" on hand-rolled one-of-n options.
                  <button
                    key={u.id}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    className={cn(
                      "flex w-full items-center gap-2 px-2 py-2 text-left text-sm transition-colors hover:bg-muted",
                      selected && "font-medium",
                    )}
                    onClick={() => {
                      onChange({ userId: u.id, label: u.full_name || u.email })
                      setOpen(false)
                      setQuery("")
                    }}
                  >
                    <Check
                      className={cn(
                        "h-3.5 w-3.5 shrink-0",
                        selected ? "opacity-100" : "opacity-0",
                      )}
                    />
                    <span className="truncate">{u.full_name || u.email}</span>
                    {u.full_name && (
                      <span className="truncate text-xs text-muted-foreground">
                        {u.email}
                      </span>
                    )}
                  </button>
                )
              })}
            </div>
          )}
        </div>
      </PopoverContent>
    </Popover>
  )
}
