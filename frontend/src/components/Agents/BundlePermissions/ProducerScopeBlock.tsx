import type { BundlePermissionProducer } from "@/client"
import { AgentApiScopeEditor } from "@/components/Common/AgentApiScopeEditor"
import { AgentBadge } from "@/components/Common/AgentBadge"

interface ProducerScopeBlockProps {
  producer: BundlePermissionProducer
  scopes: string[]
  onChange: (scopes: string[]) => void
  /** Copy shown when nothing is assigned (depends on target kind and grant). */
  emptyHint?: string
  error?: string
}

/**
 * One manageable producer's scope section in the permissions modal: the
 * producer's badge over the shared `AgentApiScopeEditor`, so this modal and
 * the producer's own Edit scopes dialog offer the same affordances.
 */
export function ProducerScopeBlock({
  producer,
  scopes,
  onChange,
  emptyHint,
  error,
}: ProducerScopeBlockProps) {
  return (
    <div className="space-y-2 rounded-md border p-3">
      <div className="flex items-center gap-2">
        <AgentBadge
          agent={{
            id: producer.producer_agent_id,
            name: producer.producer_agent_name || "Producer",
            ui_color_preset: producer.producer_ui_color_preset,
          }}
        />
        <span className="text-xs text-muted-foreground">scopes</span>
      </div>

      <AgentApiScopeEditor
        scopes={scopes}
        onChange={onChange}
        catalogScopes={producer.scope_catalog ?? []}
        emptyHint={emptyHint}
      />

      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  )
}
