import { AlertCircle, ChevronDown, ChevronRight } from "lucide-react"
import { type FormEvent, useState } from "react"

import type { ArtifactLinkCreate } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { DialogFooter } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

const HOUR = 60 * 60 * 1000

const EXPIRY_OPTIONS: { value: string; label: string; ms: number | null }[] = [
  { value: "24h", label: "24 hours", ms: 24 * HOUR },
  { value: "7d", label: "7 days", ms: 7 * 24 * HOUR },
  { value: "30d", label: "30 days", ms: 30 * 24 * HOUR },
  { value: "never", label: "Never", ms: null },
]

interface CreateLinkFormProps {
  /** The revision a pin would hold the link to (the current one). */
  currentRevision: { id: string; number: number } | null
  isCreating: boolean
  error: string | null
  onBack: () => void
  onCreate: (body: ArtifactLinkCreate) => void
}

/** The Public links segment's create body (swaps the list; not a dialog). */
export function CreateLinkForm({
  currentRevision,
  isCreating,
  error,
  onBack,
  onCreate,
}: CreateLinkFormProps) {
  const [label, setLabel] = useState("")
  const [expiry, setExpiry] = useState("7d")
  const [withCode, setWithCode] = useState(false)
  const [pin, setPin] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(false)

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (isCreating) return
    const ms = EXPIRY_OPTIONS.find((o) => o.value === expiry)?.ms ?? null
    onCreate({
      label: label.trim() || null,
      // A local instant, sent with its zone — not a wire value being parsed.
      expires_at: ms === null ? null : new Date(Date.now() + ms).toISOString(),
      with_code: withCode,
      pinned_revision_id: pin && currentRevision ? currentRevision.id : null,
    })
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      <div className="space-y-1.5">
        <Label htmlFor="artifact-link-label">
          Label{" "}
          <span className="font-normal text-muted-foreground">(optional)</span>
        </Label>
        <Input
          id="artifact-link-label"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="e.g. Board review"
          maxLength={100}
          disabled={isCreating}
          autoFocus
        />
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="artifact-link-expiry">Expires</Label>
        <Select value={expiry} onValueChange={setExpiry} disabled={isCreating}>
          <SelectTrigger id="artifact-link-expiry" className="w-full">
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

      <div className="flex items-start gap-2">
        <Checkbox
          id="artifact-link-code"
          checked={withCode}
          onCheckedChange={(v) => setWithCode(v === true)}
          disabled={isCreating}
          className="mt-0.5"
        />
        <div className="space-y-0.5">
          <Label htmlFor="artifact-link-code">Require a security code</Label>
          <p className="text-xs text-muted-foreground">
            A 4-digit code you send separately.
          </p>
        </div>
      </div>

      {currentRevision && (
        <div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="px-0 text-muted-foreground"
            onClick={() => setAdvancedOpen((prev) => !prev)}
            aria-expanded={advancedOpen}
          >
            {advancedOpen ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
            Advanced
          </Button>
          {advancedOpen && (
            <div className="flex items-start gap-2 pt-2">
              <Checkbox
                id="artifact-link-pin"
                checked={pin}
                onCheckedChange={(v) => setPin(v === true)}
                disabled={isCreating}
                className="mt-0.5"
              />
              <div className="space-y-0.5">
                <Label htmlFor="artifact-link-pin">
                  Always show revision {currentRevision.number}
                </Label>
                <p className="text-xs text-muted-foreground">
                  Otherwise the link follows new revisions.
                </p>
              </div>
            </div>
          )}
        </div>
      )}

      {error && (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertDescription className="break-words">{error}</AlertDescription>
        </Alert>
      )}

      <DialogFooter>
        <Button
          type="button"
          variant="outline"
          onClick={onBack}
          disabled={isCreating}
        >
          Back
        </Button>
        <LoadingButton type="submit" loading={isCreating}>
          {isCreating ? "Creating…" : "Create link"}
        </LoadingButton>
      </DialogFooter>
    </form>
  )
}
