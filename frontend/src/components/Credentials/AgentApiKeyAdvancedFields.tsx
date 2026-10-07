import { ChevronDown, ChevronRight } from "lucide-react"
import { useState } from "react"

import { defaultAgentApiKeySlot } from "@/components/Credentials/agentApiKeyCopy"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

/** Expiry choices, in days. ``null`` = never. */
const EXPIRY_OPTIONS: { value: string; label: string; days: number | null }[] =
  [
    { value: "never", label: "Never expires", days: null },
    { value: "7", label: "7 days", days: 7 },
    { value: "30", label: "30 days", days: 30 },
    { value: "90", label: "90 days", days: 90 },
    { value: "365", label: "1 year", days: 365 },
  ]

/** The selected expiry option as days, ``null`` for never. */
export function expiryDays(value: string): number | null {
  return EXPIRY_OPTIONS.find((o) => o.value === value)?.days ?? null
}

interface AgentApiKeyAdvancedFieldsProps {
  expiry: string
  onExpiryChange: (value: string) => void
  label: string
  onLabelChange: (value: string) => void
  slot: string
  onSlotChange: (value: string) => void
  producerName: string
}

/**
 * The Give-access dialog's one "Advanced" disclosure: expiry, name and slot —
 * the three fields that have a right default, closed by default (§1 Create).
 * The slot's placeholder is the D23 default the server applies when the field
 * is left empty; the hint shows the line of code that finds the key by it.
 */
export function AgentApiKeyAdvancedFields({
  expiry,
  onExpiryChange,
  label,
  onLabelChange,
  slot,
  onSlotChange,
  producerName,
}: AgentApiKeyAdvancedFieldsProps) {
  // Starts closed on every open: `DialogContent` unmounts on close.
  const [open, setOpen] = useState(false)

  const defaultSlot = defaultAgentApiKeySlot(producerName)

  return (
    <div>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="px-0 text-muted-foreground"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="h-4 w-4" />
        ) : (
          <ChevronRight className="h-4 w-4" />
        )}
        Advanced
      </Button>
      {open && (
        <div className="space-y-4 pt-2">
          <div className="space-y-1.5">
            <Label htmlFor="agent-api-key-expiry">Expires</Label>
            <Select value={expiry} onValueChange={onExpiryChange}>
              <SelectTrigger id="agent-api-key-expiry" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {EXPIRY_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value}>
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="agent-api-key-name">Name</Label>
            <Input
              id="agent-api-key-name"
              value={label}
              onChange={(e) => onLabelChange(e.target.value)}
              placeholder={`${producerName || "Agent"} API key`}
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="agent-api-key-slot">Slot</Label>
            <Input
              id="agent-api-key-slot"
              value={slot}
              onChange={(e) => onSlotChange(e.target.value)}
              placeholder={defaultSlot}
              className="font-mono"
            />
            <p className="text-xs text-muted-foreground break-words">
              Code finds the key by this name:{" "}
              <code className="font-mono">
                {`agent_api_session("${slot.trim() || defaultSlot}")`}
              </code>
              . The recipient can change it.
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
