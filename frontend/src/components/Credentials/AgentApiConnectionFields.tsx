import {
  AgentApiScopeEditor,
  type ScopeCatalogEntry,
} from "@/components/Common/AgentApiScopeEditor"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { defaultAgentApiKeySlot } from "@/components/Credentials/agentApiKeyCopy"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

/** What an empty connection scope list means, on create and on edit. */
export const CONNECTION_SCOPES_EMPTY_HINT =
  "No scopes — callers get only the scopes their own grant gives them."

interface AgentApiConnectionFieldsProps {
  scopes: string[]
  onScopesChange: (scopes: string[]) => void
  catalogScopes: ScopeCatalogEntry[]
  /** The catalog has loaded and is empty: the API declares no scopes. */
  catalogEmpty: boolean
  /** No producer chosen yet — scope names are per-producer. */
  scopesDisabled: boolean
  /** The server's ``unknown_scope`` refusal, shown under the editor. */
  scopesError?: string | null
  /** Names the API declares, once the catalog has loaded. */
  declaredScopes?: ReadonlySet<string>
  /** The catalog failed to load. */
  catalogError?: unknown
  onRetryCatalog?: () => void
  name: string
  onNameChange: (value: string) => void
  slot: string
  onSlotChange: (value: string) => void
  producerName: string
  disabled?: boolean
}

/** The name a connection gets when the field is left empty. */
export function defaultAgentApiConnectionName(producerName: string): string {
  return `${producerName || "Agent"} API`
}

/**
 * Connection mode's fields: the scopes every caller gets, then name and slot.
 * No recipient or consumer: a connection is shared afterwards from its own
 * page, and every caller is identified as themselves. Scopes come from the
 * producer's catalog only (the server rejects anything else). The slot has **no** default on the server for a connection, so
 * its placeholder is phrased as a suggestion ("e.g. …"), not as a value that
 * will be applied.
 */
export function AgentApiConnectionFields({
  scopes,
  onScopesChange,
  catalogScopes,
  catalogEmpty,
  scopesDisabled,
  scopesError,
  declaredScopes,
  catalogError,
  onRetryCatalog,
  name,
  onNameChange,
  slot,
  onSlotChange,
  producerName,
  disabled,
}: AgentApiConnectionFieldsProps) {
  const suggestedSlot = defaultAgentApiKeySlot(producerName)
  const hasUndeclared =
    !!declaredScopes && scopes.some((s) => !declaredScopes.has(s))

  return (
    <>
      <div className="space-y-1.5">
        <Label>Scopes (optional)</Label>
        <AgentApiScopeEditor
          scopes={scopes}
          onChange={onScopesChange}
          catalogScopes={catalogScopes}
          emptyHint={CONNECTION_SCOPES_EMPTY_HINT}
          allowCustom={false}
          declaredScopes={declaredScopes}
          disabled={scopesDisabled || disabled}
        />
        {catalogError ? (
          <QueryErrorAlert
            error={catalogError}
            fallback="Couldn't load the scopes this API declares"
            onRetry={onRetryCatalog}
            compact
          />
        ) : null}
        {hasUndeclared && (
          <p className="text-xs text-muted-foreground">
            Remove scopes the API doesn't declare to create the connection.
          </p>
        )}
        {scopesError && (
          <p className="text-xs text-destructive break-words">{scopesError}</p>
        )}
        <p className="text-xs text-muted-foreground break-words">
          {catalogEmpty ? (
            <>
              This API declares no scopes yet. Add{" "}
              <code className="font-mono">scopes:</code> to its policy.yaml and
              refresh the API.
            </>
          ) : (
            "Everyone using this connection gets these, including people you share it with. You can change them later on the connection's page."
          )}
        </p>
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="agent-api-connection-name">Name</Label>
        <Input
          id="agent-api-connection-name"
          value={name}
          maxLength={255}
          onChange={(e) => onNameChange(e.target.value)}
          placeholder={defaultAgentApiConnectionName(producerName)}
          disabled={disabled}
        />
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="agent-api-connection-slot">Slot (optional)</Label>
        <Input
          id="agent-api-connection-slot"
          value={slot}
          maxLength={255}
          onChange={(e) => onSlotChange(e.target.value)}
          placeholder={`e.g. ${suggestedSlot}`}
          className="font-mono"
          disabled={disabled}
        />
        {/* No server default for a connection's slot: an empty field means
            no slot, so the hint never names a value that won't be applied. */}
        <p className="text-xs text-muted-foreground break-words">
          {slot.trim()
            ? "Bundle and skill code finds the connection with "
            : "Set a slot so bundle and skill code can find the connection with "}
          <code className="font-mono">
            {`agent_api_session("${slot.trim() || "<slot>"}")`}
          </code>
          .
        </p>
      </div>
    </>
  )
}
