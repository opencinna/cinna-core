import type { AICredentialTestResult } from "@/client"
import { ListModelsButton } from "@/components/Common/ListModelsButton"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { stripProviderPrefix } from "./providerTypes"

interface ModelOverrideFieldProps {
  /** Unique id for the input, so its `Label` addresses the right one. */
  id: string
  /** "Conversation" / "Building". */
  modeLabel: string
  value: string
  onChange: (value: string) => void
  /** The value already stored on the record; `null` when there is none yet. */
  storedValue: string | null
  /** Fetch a live model list for the picker. */
  probeModels: () => Promise<AICredentialTestResult>
  /** True while there is nothing to probe with, or a save is in flight. */
  probeDisabled: boolean
  disabled?: boolean
}

/**
 * One mode's model override, with the shared model picker beside it.
 *
 * Controlled rather than bound to `react-hook-form`, because its three hosts
 * store their forms differently: the managed-credential dialog is an RHF form,
 * and the two provider surfaces hold plain state (their key section's fields
 * are defined at runtime by the adapter's `admin_config_schema`, so a static
 * resolver would have to restate a list the server just sent). One component
 * with a `value`/`onChange` pair is what lets all three render the same field
 * and the same warning; a second copy is how the "saving unpins" sentence ends
 * up on one surface and not the other.
 *
 * `ListModelsButton` is a `Popover`, not a `Dialog`, so embedding it inside a
 * dialog or a sheet does not add a disclosure level (guidelines §2).
 */
export function ModelOverrideField({
  id,
  modeLabel,
  value,
  onChange,
  storedValue,
  probeModels,
  probeDisabled,
  disabled,
}: ModelOverrideFieldProps) {
  // Emptying the box is a real edit: the submit handler sends `""`, the
  // backend stores NULL and unpins every member still carrying the value being
  // dropped. Say what that costs while the box is empty, since the consequence
  // lands on other people's profiles rather than on this screen.
  const clearing = !!storedValue && value.trim() === ""

  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{modeLabel} model override</Label>
      <div className="flex items-start gap-2">
        <Input
          id={id}
          placeholder="Leave blank to use the provider's default model"
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        />
        <ListModelsButton
          credentialId={null}
          credentialType={null}
          probeModels={probeModels}
          disabled={probeDisabled}
          // The picker hands back the provider's id verbatim, and the backend
          // stores `_normalize_default_model` of it. Stripping here rather than
          // only on submit keeps the box showing the value that will be saved.
          onSelect={(modelId) => onChange(stripProviderPrefix(modelId))}
        />
      </div>
      <p className="text-xs text-muted-foreground">
        Pinned as each member's {modeLabel.toLowerCase()} model when this
        becomes their default for that mode. Leave blank for no opinion —
        members fall back to the default model below.
      </p>
      {clearing && (
        <p className="text-xs text-warning">
          Saving unpins "{storedValue}" from members who still have it. Anyone
          who picked their own model keeps it.
        </p>
      )}
    </div>
  )
}
