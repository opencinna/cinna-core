import { Search } from "lucide-react"

import { ListRowGroup } from "@/components/Common/ListRow"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { TooltipToggleItem } from "@/components/Common/TooltipToggleItem"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup } from "@/components/ui/toggle-group"
import type { AddAddonKind, AddAddonResult } from "@/utils/addons"
import { AddAddonResultRow } from "./AddAddonResultRow"

const KIND_SEGMENTS: Array<{
  value: AddAddonKind
  label: string
  hint: string
}> = [
  { value: "all", label: "All", hint: "Plugins and skills" },
  { value: "plugin", label: "Plugins", hint: "Marketplace plugins only" },
  {
    value: "skill",
    label: "Skills",
    // Both kinds of skill: the catalog's packages and a skills-format
    // marketplace's entries. The segment names the thing, not the source it
    // arrives from.
    hint: "Skills, from the catalog or a marketplace",
  },
]

interface AddAddonChooseStepProps {
  query: string
  onQueryChange: (next: string) => void
  /** The debounced query, for the "no match" sentence. */
  debouncedQuery: string
  kind: AddAddonKind
  onKindChange: (next: AddAddonKind) => void
  results: AddAddonResult[]
  /** The fold returned more than the cap, so the list below is truncated. */
  isCapped: boolean
  resultLimit: number
  selectedKey: string | null
  onSelect: (result: AddAddonResult) => void
  isLoading: boolean
  disabled: boolean
  /** The addons projection failed: nothing can be marked already-installed. */
  addonsUnavailable: boolean
  addonsError: unknown
  onRetryAddons: () => void
  /** Both searches failed — there is no list to render at all. */
  bothFailed: boolean
  /** One search failed: the other half still renders, with a named gap. */
  halfFailedMessage: string | null
  searchError: unknown
  onRetrySearch: () => void
}

/**
 * Step 1 of the Add addon wizard: find the thing.
 *
 * A display component over state the dialog owns — the search box, the kind
 * filter, the three failure branches and the capped result list. Split out of
 * `AddAddonDialog` so the dialog is left with the state, the install mutation
 * and the stepper; the fold that produces `results` is `buildAddonResults` in
 * `utils/addons.ts`, which is pure and testable without rendering any of this.
 */
export function AddAddonChooseStep({
  query,
  onQueryChange,
  debouncedQuery,
  kind,
  onKindChange,
  results,
  isCapped,
  resultLimit,
  selectedKey,
  onSelect,
  isLoading,
  disabled,
  addonsUnavailable,
  addonsError,
  onRetryAddons,
  bothFailed,
  halfFailedMessage,
  searchError,
  onRetrySearch,
}: AddAddonChooseStepProps) {
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <Search className="absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            className="pl-9"
            placeholder="Search by name, description or publisher…"
            value={query}
            disabled={disabled}
            onChange={(e) => onQueryChange(e.target.value)}
          />
        </div>
        <ToggleGroup
          type="single"
          variant="outline"
          size="sm"
          value={kind}
          disabled={disabled}
          onValueChange={(next) => next && onKindChange(next as AddAddonKind)}
          className="shrink-0"
          aria-label="Which kinds to show"
        >
          {KIND_SEGMENTS.map((segment) => (
            <TooltipToggleItem
              key={segment.value}
              value={segment.value}
              label={segment.hint}
            >
              {segment.label}
            </TooltipToggleItem>
          ))}
        </ToggleGroup>
      </div>

      {addonsUnavailable ? (
        // A list of installable rows that may re-install what the agent already
        // has is worse than no list, so it is not rendered until the projection
        // succeeds.
        <QueryErrorAlert
          error={addonsError}
          fallback="Couldn't check what is already installed"
          onRetry={onRetryAddons}
        >
          <p className="text-xs">
            Results stay hidden until that succeeds, so nothing already
            installed is offered again.
          </p>
        </QueryErrorAlert>
      ) : bothFailed ? (
        <QueryErrorAlert
          error={searchError}
          fallback="Couldn't search the marketplaces or the skills catalog"
          onRetry={onRetrySearch}
        />
      ) : (
        <>
          {halfFailedMessage && (
            <Alert>
              <AlertDescription className="flex items-center justify-between gap-3">
                <span>{halfFailedMessage}</span>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 shrink-0 text-xs"
                  onClick={onRetrySearch}
                >
                  Try again
                </Button>
              </AlertDescription>
            </Alert>
          )}

          <div className="max-h-[45vh] overflow-y-auto">
            {isLoading ? (
              <div className="space-y-1.5">
                <Skeleton className="h-[48px] w-full rounded-md" />
                <Skeleton className="h-[48px] w-full rounded-md" />
                <Skeleton className="h-[48px] w-full rounded-md" />
              </div>
            ) : results.length === 0 ? (
              debouncedQuery ? (
                <div className="py-8 text-center">
                  <p className="text-sm text-muted-foreground">
                    No plugins or skills match “{debouncedQuery}”.
                  </p>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="mt-1"
                    onClick={() => onQueryChange("")}
                  >
                    Clear search
                  </Button>
                </div>
              ) : (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  No plugins or skills are available yet. Ask an admin to add an
                  addon marketplace.
                </p>
              )
            ) : (
              // The rows are `role="option"`, so they need an owning listbox;
              // `ListRowGroup` is the generic between them, which the
              // accessibility tree flattens.
              <div role="listbox" aria-label="Search results">
                <ListRowGroup>
                  {results.map((result) => (
                    <AddAddonResultRow
                      key={result.key}
                      result={result}
                      selected={selectedKey === result.key}
                      onSelect={() => onSelect(result)}
                    />
                  ))}
                </ListRowGroup>
              </div>
            )}
          </div>

          {isCapped && (
            <p className="text-xs text-muted-foreground">
              Showing the first {resultLimit} — refine your search.
            </p>
          )}
        </>
      )}
    </div>
  )
}
