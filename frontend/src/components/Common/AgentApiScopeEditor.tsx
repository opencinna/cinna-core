import { Plus, X } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

/**
 * What an empty scope list means, by whose grant it is: a person's empty
 * grant still identifies them; a group's identifies its members but adds no
 * scopes; with no grant at all, saving nothing writes nothing.
 */
export const SCOPE_EMPTY_HINTS = {
  person: "No scopes — the user is identified but carries no capabilities.",
  group:
    "No scopes — members are identified here, but gain no scopes from this group.",
  none: "No scopes — nothing is granted on this producer.",
} as const

export type ScopeCatalogEntry = { name: string; description?: string | null }

interface AgentApiScopeEditorProps {
  /** Currently assigned scope names. */
  scopes: string[]
  onChange: (scopes: string[]) => void
  /** Scopes the producer declared in ``policy.yaml`` (quick-add chips). */
  catalogScopes: ScopeCatalogEntry[]
  /** Copy shown when nothing is assigned. */
  emptyHint?: string
  disabled?: boolean
  /**
   * Offer the free-text row. Connections pass `false`: their scopes are
   * validated strictly against the catalog, so a typed name could only fail.
   */
  allowCustom?: boolean
  /**
   * Names the producer still declares. When given, an assigned scope outside
   * it is marked as no longer declared. Omit while the catalog is unknown.
   */
  declaredScopes?: ReadonlySet<string>
}

/**
 * The agent-api scope chip editor: removable assigned chips, quick-add from the
 * producer's ``policy.yaml`` catalog, and a free-text fallback (the catalog is
 * empty until the producer declares ``scopes:``). Connections turn the
 * fallback off (``allowCustom={false}``) and mark undeclared scopes.
 *
 * Shared by the producer's "Access & Scopes" card and an external key's
 * credential detail card — both edit the SAME ``agent_api_access_grant`` row
 * (plan D5), so they must offer the same affordances.
 */
export function AgentApiScopeEditor({
  scopes,
  onChange,
  catalogScopes,
  emptyHint = SCOPE_EMPTY_HINTS.person,
  disabled,
  allowCustom = true,
  declaredScopes,
}: AgentApiScopeEditorProps) {
  const [customScope, setCustomScope] = useState("")
  const rootRef = useRef<HTMLDivElement>(null)
  // A remove or quick-add button unmounts with its chip. Where focus goes
  // next is decided at the click and applied once the chips re-render.
  const pendingFocus = useRef<{ kind: "remove" | "add"; index: number } | null>(
    null,
  )

  useEffect(() => {
    const target = pendingFocus.current
    const root = rootRef.current
    if (!target || !root) return
    pendingFocus.current = null
    const removes = root.querySelectorAll<HTMLElement>("[data-scope-remove]")
    const adds = root.querySelectorAll<HTMLElement>("[data-scope-add]")
    const primary = target.kind === "remove" ? removes : adds
    const next =
      primary[Math.min(target.index, primary.length - 1)] ??
      (target.kind === "remove" ? adds[0] : removes[removes.length - 1]) ??
      root
    next.focus()
  })

  const toggleScope = (scope: string) =>
    onChange(
      scopes.includes(scope)
        ? scopes.filter((s) => s !== scope)
        : [...scopes, scope],
    )

  const removeScope = (scope: string, index: number) => {
    pendingFocus.current = { kind: "remove", index }
    toggleScope(scope)
  }

  const quickAddScope = (scope: string, index: number) => {
    pendingFocus.current = { kind: "add", index }
    toggleScope(scope)
  }

  const addCustomScope = () => {
    const value = customScope.trim()
    if (value && !scopes.includes(value)) {
      onChange([...scopes, value])
    }
    setCustomScope("")
  }

  const unassignedCatalog = catalogScopes.filter(
    (s) => !scopes.includes(s.name),
  )

  return (
    <div ref={rootRef} tabIndex={-1} className="space-y-2 outline-none">
      {/* Assigned scopes (removable chips) */}
      <div className="flex flex-wrap gap-1.5">
        {scopes.length === 0 ? (
          <span className="text-xs text-muted-foreground italic">
            {emptyHint}
          </span>
        ) : (
          scopes.map((scope, index) => {
            const undeclared = !!declaredScopes && !declaredScopes.has(scope)
            const chip = (
              <Badge
                key={scope}
                variant={undeclared ? "outline" : "secondary"}
                className={
                  undeclared ? "gap-1 text-xs text-warning" : "gap-1 text-xs"
                }
              >
                {scope}
                <button
                  type="button"
                  onClick={() => removeScope(scope, index)}
                  disabled={disabled}
                  data-scope-remove=""
                  className="hover:text-destructive transition-colors disabled:opacity-50"
                  aria-label={
                    undeclared
                      ? `Remove scope ${scope} (not declared)`
                      : `Remove scope ${scope}`
                  }
                >
                  <X className="h-3 w-3" />
                </button>
              </Badge>
            )
            if (!undeclared) return chip
            return (
              <Tooltip key={scope}>
                <TooltipTrigger asChild>{chip}</TooltipTrigger>
                <TooltipContent>Not declared in policy.yaml</TooltipContent>
              </Tooltip>
            )
          })
        )}
      </div>

      {/* Quick-add from the policy.yaml catalog */}
      {unassignedCatalog.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {unassignedCatalog.map((s, index) => {
            const chip = (
              <button
                key={s.name}
                type="button"
                onClick={() => quickAddScope(s.name, index)}
                disabled={disabled}
                data-scope-add=""
                className="inline-flex items-center gap-1 rounded-full border border-dashed px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent transition-colors disabled:opacity-50"
              >
                <Plus className="h-3 w-3" />
                {s.name}
              </button>
            )
            if (!s.description) return chip
            return (
              <Tooltip key={s.name}>
                <TooltipTrigger asChild>{chip}</TooltipTrigger>
                <TooltipContent className="max-w-xs break-words">
                  {s.description}
                </TooltipContent>
              </Tooltip>
            )
          })}
        </div>
      )}

      {/* Free-text scope add */}
      {allowCustom && (
        <div className="flex items-center gap-1.5">
          <Input
            value={customScope}
            onChange={(e) => setCustomScope(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault()
                addCustomScope()
              }
            }}
            placeholder="Add a scope name..."
            className="h-8 text-xs"
            disabled={disabled}
          />
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 shrink-0"
            onClick={addCustomScope}
            disabled={disabled || !customScope.trim()}
          >
            Add
          </Button>
        </div>
      )}
    </div>
  )
}
