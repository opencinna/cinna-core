import { Link } from "@tanstack/react-router"
import { AlertTriangle } from "lucide-react"

import {
  type AgentOption,
  AgentSelectorList,
} from "@/components/Common/AgentSelectorDialog"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"

interface AgentApiProducerFieldProps {
  options: AgentOption[]
  selectedId: string
  onSelect: (id: string) => void
  isLoading: boolean
  isError: boolean
  error: unknown
  onRetry: () => void
  /** The dialog is open — the picker's popover listens only then. */
  active: boolean
  disabled?: boolean
  /**
   * The chosen producer when its external access is off and that matters (key
   * mode only — a connection is not gated by it): says where to turn it on.
   */
  externalAccessOffFor?: { id: string; name: string }
}

/**
 * The Give-access dialog's producer picker, shown on `/credentials` › New only
 * (the producer's own API access card fixes the producer and hides this).
 * Shared by both modes: a key and a connection are minted on the same agents.
 */
export function AgentApiProducerField({
  options,
  selectedId,
  onSelect,
  isLoading,
  isError,
  error,
  onRetry,
  active,
  disabled,
  externalAccessOffFor: off,
}: AgentApiProducerFieldProps) {
  return (
    <fieldset className="space-y-1.5">
      <Label asChild>
        <legend>Producer</legend>
      </Label>
      {isError ? (
        <QueryErrorAlert
          error={error}
          fallback="Couldn't load your agents"
          onRetry={onRetry}
          compact
        />
      ) : isLoading ? (
        <Skeleton className="h-9 w-full rounded-md" />
      ) : options.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          None of your agents expose a REST API yet.
        </p>
      ) : (
        <AgentSelectorList
          mode="single"
          agents={options}
          selectedAgentId={selectedId || null}
          onSelect={onSelect}
          placeholder="Pick an agent that exposes a REST API"
          active={active}
          disabled={disabled}
        />
      )}
      {off && (
        <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
          <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0 text-warning" />
          <span>
            External access is off for {off.name}. Turn it on in its{" "}
            <Link
              to="/agent/$agentId"
              params={{ agentId: off.id }}
              hash="integrations"
              className="text-primary hover:underline"
            >
              API access card
            </Link>{" "}
            first.
          </span>
        </p>
      )}
    </fieldset>
  )
}
