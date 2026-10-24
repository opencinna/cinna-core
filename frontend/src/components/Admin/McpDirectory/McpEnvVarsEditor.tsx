import { Plus, X } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { type McpEnvRow, newEnvRow } from "./mcpEntryDraft"

interface McpEnvVarsEditorProps {
  value: McpEnvRow[]
  onChange: (next: McpEnvRow[]) => void
  /** Per row, keyed by `McpEnvRow.key`. */
  errors?: Record<string, string>
  /** The list as a whole is over the cap. */
  countError?: string
  idPrefix: string
}

/**
 * The environment variables a server needs from the user — names only, never
 * values. A repeating row of Name · Description · Secret · remove, in a capped
 * scrolling container (`pr-2`, so the remove button is not under the
 * scrollbar). Shared by the Add wizard's step 2 and the Edit Sheet.
 */
export function McpEnvVarsEditor({
  value,
  onChange,
  errors,
  countError,
  idPrefix,
}: McpEnvVarsEditorProps) {
  const addRef = useRef<HTMLButtonElement>(null)
  const nameRefs = useRef(new Map<string, HTMLInputElement>())
  const [focusKey, setFocusKey] = useState<string | null>(null)

  // A new row is focused once it has rendered.
  useEffect(() => {
    if (!focusKey) return
    nameRefs.current.get(focusKey)?.focus()
    setFocusKey(null)
  }, [focusKey])

  const update = (key: string, patch: Partial<McpEnvRow>) =>
    onChange(value.map((row) => (row.key === key ? { ...row, ...patch } : row)))

  const add = () => {
    const row = newEnvRow()
    onChange([...value, row])
    setFocusKey(row.key)
  }

  const remove = (key: string) => {
    onChange(value.filter((row) => row.key !== key))
    // The remove button unmounts with its row; focus goes somewhere stable.
    addRef.current?.focus()
  }

  return (
    <div className="space-y-2">
      <div>
        <p className="text-sm font-medium">Environment variables</p>
        <p className="text-xs text-muted-foreground">
          Values the user supplies when they add the server. Only the names are
          stored here.
        </p>
      </div>

      {value.length > 0 && (
        <div className="max-h-[40vh] space-y-2 overflow-y-auto pr-2">
          {value.map((row, index) => {
            const error = errors?.[row.key]
            const nameId = `${idPrefix}-env-${row.key}-name`
            const secretId = `${idPrefix}-env-${row.key}-secret`
            return (
              <div key={row.key} className="space-y-1">
                <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)_auto_auto] items-center gap-2">
                  <Input
                    id={nameId}
                    ref={(node) => {
                      if (node) nameRefs.current.set(row.key, node)
                      else nameRefs.current.delete(row.key)
                    }}
                    value={row.name}
                    onChange={(e) =>
                      update(row.key, { name: e.target.value.toUpperCase() })
                    }
                    placeholder="API_KEY"
                    aria-label={`Variable ${index + 1} name`}
                    aria-invalid={error ? true : undefined}
                    className="h-8 font-mono text-xs"
                    spellCheck={false}
                    autoComplete="off"
                  />
                  <Input
                    value={row.description}
                    onChange={(e) =>
                      update(row.key, { description: e.target.value })
                    }
                    placeholder="Description"
                    aria-label={`Variable ${index + 1} description`}
                    className="h-8 text-xs"
                  />
                  <div className="flex items-center gap-1.5">
                    <Checkbox
                      id={secretId}
                      checked={row.secret}
                      onCheckedChange={(checked) =>
                        update(row.key, { secret: checked === true })
                      }
                    />
                    <label htmlFor={secretId} className="text-xs">
                      Secret
                    </label>
                  </div>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7"
                        onClick={() => remove(row.key)}
                        aria-label={`Remove variable ${row.name || index + 1}`}
                      >
                        <X className="h-3.5 w-3.5" />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent side="top" className="text-xs">
                      Remove variable
                    </TooltipContent>
                  </Tooltip>
                </div>
                {error && <p className="text-xs text-destructive">{error}</p>}
              </div>
            )
          })}
        </div>
      )}

      {countError && <p className="text-xs text-destructive">{countError}</p>}
      <Button
        ref={addRef}
        type="button"
        variant="link"
        size="sm"
        className="h-auto gap-1 px-0"
        onClick={add}
      >
        <Plus className="h-3.5 w-3.5" />
        Add variable
      </Button>
    </div>
  )
}
