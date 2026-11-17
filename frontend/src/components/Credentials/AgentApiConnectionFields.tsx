import { defaultAgentApiKeySlot } from "@/components/Credentials/agentApiKeyCopy"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

interface AgentApiConnectionFieldsProps {
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
 * Connection mode's two fields. No recipient, scopes or consumer: a connection
 * is shared afterwards from its own page, and every caller is identified as
 * themselves. The slot has **no** default on the server for a connection, so
 * its placeholder is phrased as a suggestion ("e.g. …"), not as a value that
 * will be applied.
 */
export function AgentApiConnectionFields({
  name,
  onNameChange,
  slot,
  onSlotChange,
  producerName,
  disabled,
}: AgentApiConnectionFieldsProps) {
  const suggestedSlot = defaultAgentApiKeySlot(producerName)

  return (
    <>
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
