import { Plus, X } from "lucide-react"
import { useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"

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
}

/**
 * The agent-api scope chip editor: removable assigned chips, quick-add from the
 * producer's ``policy.yaml`` catalog, and a free-text fallback (the catalog is
 * empty until the producer declares ``scopes:``).
 *
 * Shared by the producer's "Access & Scopes" card and an external key's
 * credential detail card — both edit the SAME ``agent_api_access_grant`` row
 * (plan D5), so they must offer the same affordances.
 */
export function AgentApiScopeEditor({
  scopes,
  onChange,
  catalogScopes,
  emptyHint = "No scopes — the user is identified but carries no capabilities.",
  disabled,
}: AgentApiScopeEditorProps) {
  const [customScope, setCustomScope] = useState("")

  const toggleScope = (scope: string) =>
    onChange(
      scopes.includes(scope)
        ? scopes.filter((s) => s !== scope)
        : [...scopes, scope],
    )

  const addCustomScope = () => {
    const value = customScope.trim()
    if (value && !scopes.includes(value)) {
      onChange([...scopes, value])
    }
    setCustomScope("")
  }

  const unassignedCatalog = catalogScopes.filter((s) => !scopes.includes(s.name))

  return (
    <div className="space-y-2">
      {/* Assigned scopes (removable chips) */}
      <div className="flex flex-wrap gap-1.5">
        {scopes.length === 0 ? (
          <span className="text-xs text-muted-foreground italic">
            {emptyHint}
          </span>
        ) : (
          scopes.map((scope) => (
            <Badge key={scope} variant="secondary" className="gap-1 text-xs">
              {scope}
              <button
                type="button"
                onClick={() => toggleScope(scope)}
                disabled={disabled}
                className="hover:text-destructive transition-colors disabled:opacity-50"
                aria-label={`Remove scope ${scope}`}
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          ))
        )}
      </div>

      {/* Quick-add from the policy.yaml catalog */}
      {unassignedCatalog.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {unassignedCatalog.map((s) => (
            <button
              key={s.name}
              type="button"
              onClick={() => toggleScope(s.name)}
              disabled={disabled}
              title={s.description ?? undefined}
              className="inline-flex items-center gap-1 rounded-full border border-dashed px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent transition-colors disabled:opacity-50"
            >
              <Plus className="h-3 w-3" />
              {s.name}
            </button>
          ))}
        </div>
      )}

      {/* Free-text scope add */}
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
    </div>
  )
}
