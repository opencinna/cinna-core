import { Info } from "lucide-react"

import { Button } from "@/components/ui/button"

/**
 * What the Give-access dialog makes: a personal **key** for one person, or a
 * shareable **connection** other people's agents (and bundles) call through.
 */
export type AgentApiAccessMode = "key" | "connection"

interface AgentApiAccessModeInfoProps {
  mode: AgentApiAccessMode
  onModeChange: (mode: AgentApiAccessMode) => void
  disabled?: boolean
}

/**
 * The two-line "key or connection?" explainer at the top of the dialog, with
 * the one link that switches to the other. Users kept reaching for a key when
 * they meant to let someone else's bundle call the API, and a key can never be
 * shared — so both definitions are always on screen, side by side, rather than
 * only the one for the current mode.
 */
export function AgentApiAccessModeInfo({
  mode,
  onModeChange,
  disabled,
}: AgentApiAccessModeInfoProps) {
  return (
    <div className="flex gap-2.5 rounded-md border bg-muted/40 p-3 text-xs text-muted-foreground">
      <Info className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <div className="min-w-0 space-y-1.5 break-words">
        <p>
          <span className="font-medium text-foreground">A key</span> is personal
          access for one person: their scripts, Cinna Desktop, their own agents.
          It carries their identity and scopes, and can't be shared.
        </p>
        <p>
          <span className="font-medium text-foreground">A connection</span> is
          what you share with other people, so their agents — and bundles they
          publish, if you allow re-use — can call this API. Each caller is still
          identified, and gets the scopes you set on the connection.
        </p>
        <Button
          type="button"
          variant="link"
          size="sm"
          className="h-auto p-0 text-xs"
          disabled={disabled}
          onClick={() => onModeChange(mode === "key" ? "connection" : "key")}
        >
          {mode === "key"
            ? "Create a shareable connection instead"
            : "Give a personal key instead"}
        </Button>
      </div>
    </div>
  )
}
