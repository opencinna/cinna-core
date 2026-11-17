import { AlertTriangle } from "lucide-react"
import {
  AgentApiScopeEditor,
  type ScopeCatalogEntry,
} from "@/components/Common/AgentApiScopeEditor"
import {
  UserSelectField,
  type UserSelectValue,
} from "@/components/Common/UserSelectField"
import { AgentApiKeyAdvancedFields } from "@/components/Credentials/AgentApiKeyAdvancedFields"

import { Label } from "@/components/ui/label"

interface AgentApiKeyFieldsProps {
  recipient: UserSelectValue | null
  onRecipientChange: (value: UserSelectValue | null) => void
  recipientIsMe: boolean
  scopes: string[]
  onScopesChange: (scopes: string[]) => void
  catalogScopes: ScopeCatalogEntry[]
  expiry: string
  onExpiryChange: (value: string) => void
  label: string
  onLabelChange: (value: string) => void
  slot: string
  onSlotChange: (value: string) => void
  producerName: string
  /** No producer chosen yet — scope names are per-producer. */
  scopesDisabled: boolean
  /**
   * The fixed producer (API access card) has External access off: reached by
   * switching from connection mode, since the card's own button is disabled.
   */
  externalAccessOffFixed?: boolean
  disabled: boolean
}

/**
 * Key mode's fields: who the key is issued to, the scopes their grant carries,
 * and the Advanced disclosure (expiry, name, slot). State lives in the dialog,
 * which also owns the connection mode these share the name and slot with.
 */
export function AgentApiKeyFields({
  recipient,
  onRecipientChange,
  recipientIsMe,
  scopes,
  onScopesChange,
  catalogScopes,
  expiry,
  onExpiryChange,
  label,
  onLabelChange,
  slot,
  onSlotChange,
  producerName,
  scopesDisabled,
  externalAccessOffFixed,
  disabled,
}: AgentApiKeyFieldsProps) {
  const recipientName = recipient?.label || "They"

  return (
    <>
      {externalAccessOffFixed && (
        <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
          <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0 text-warning" />
          <span>
            External access is off, so keys don't work. Turn it on in the API
            access card first.
          </span>
        </p>
      )}
      <div className="space-y-1.5">
        <Label htmlFor="agent-api-key-recipient">Issued to</Label>
        <UserSelectField
          id="agent-api-key-recipient"
          value={recipient}
          onChange={onRecipientChange}
          includeSelf
          disabled={disabled}
        />
        {recipient && !recipientIsMe && (
          <p className="text-xs text-muted-foreground break-words">
            The key goes to {recipientName}'s credentials. You won't see its
            value, and you can revoke it anytime.
          </p>
        )}
      </div>

      <div className="space-y-1.5">
        <Label>Scopes (optional)</Label>
        <AgentApiScopeEditor
          scopes={scopes}
          onChange={onScopesChange}
          catalogScopes={catalogScopes}
          emptyHint="No scopes — the caller is identified but carries no capabilities."
          disabled={scopesDisabled || disabled}
        />
        <p className="text-xs text-muted-foreground">
          {recipientIsMe || !recipient
            ? "Shared by every key you hold on this agent. Leave empty to keep what you have."
            : `Shared by every key ${recipientName} holds on this agent. Leave empty to keep what they have.`}
        </p>
      </div>

      <AgentApiKeyAdvancedFields
        expiry={expiry}
        onExpiryChange={onExpiryChange}
        label={label}
        onLabelChange={onLabelChange}
        slot={slot}
        onSlotChange={onSlotChange}
        producerName={producerName}
      />
    </>
  )
}
