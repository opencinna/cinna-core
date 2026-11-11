import { useState } from "react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import { MAX_MONTHLY_MINUTES, useVoiceConfigUpdate } from "./voiceTranscription"

/** `null` when the text is a valid limit, otherwise the sentence to show. */
function validate(raw: string): string | null {
  const trimmed = raw.trim()
  if (trimmed === "") return null
  if (!/^\d+$/.test(trimmed)) return "Enter a whole number of minutes."
  const value = Number(trimmed)
  if (value < 1 || value > MAX_MONTHLY_MINUTES) {
    return `Enter between 1 and ${MAX_MONTHLY_MINUTES.toLocaleString("en-US")} minutes.`
  }
  return null
}

/**
 * The per-person monthly dictation limit, edited as a one-field form.
 *
 * Empty means no limit and travels as an explicit `null` — the server reads a
 * present `null` on this field as "clear it". A refused save stays in the
 * dialog under the field rather than in a toast behind it.
 */
export function VoiceMonthlyLimitDialog({
  open,
  onOpenChange,
  persisted,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The saved limit in minutes; `null` is no limit. */
  persisted: number | null
}) {
  const persistedText = persisted == null ? "" : String(persisted)
  const [draft, setDraft] = useState<string | null>(null)
  const [serverError, setServerError] = useState<string | null>(null)

  const value = draft ?? persistedText
  const dirty = value.trim() !== persistedText
  const validationError = validate(value)
  const error = validationError ?? serverError

  const close = () => {
    setDraft(null)
    setServerError(null)
    onOpenChange(false)
  }

  const mutation = useVoiceConfigUpdate({
    onSaved: close,
    onError: setServerError,
  })

  const save = () => {
    if (validationError) return
    setServerError(null)
    const trimmed = value.trim()
    mutation.mutate({
      voice_transcription_monthly_minutes_per_user:
        trimmed === "" ? null : Number(trimmed),
    })
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => (next ? onOpenChange(true) : close())}
    >
      <DialogContent
        className="sm:max-w-md"
        onEscapeKeyDown={(event) => {
          if (mutation.isPending) event.preventDefault()
        }}
        onInteractOutside={(event) => {
          if (mutation.isPending) event.preventDefault()
        }}
      >
        <DialogHeader>
          <DialogTitle>Monthly limit per person</DialogTitle>
          <DialogDescription>
            Minutes of dictation each person may use per calendar month (UTC).
            Leave empty for no limit.
          </DialogDescription>
        </DialogHeader>

        <form
          className="space-y-2 py-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (dirty && !mutation.isPending) save()
          }}
        >
          <Label htmlFor="voice-monthly-limit" className="text-sm font-medium">
            Minutes
          </Label>
          <Input
            id="voice-monthly-limit"
            // Text, not `number`: a number input reports a half-typed entry
            // ("1e", "-") as "", which would read as "no limit" and clear it.
            type="text"
            inputMode="numeric"
            placeholder="No limit"
            value={value}
            onChange={(event) => {
              setDraft(event.target.value)
              if (serverError) setServerError(null)
            }}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "voice-monthly-limit-error" : undefined}
          />
          {error && (
            <p
              id="voice-monthly-limit-error"
              role="alert"
              className="text-xs text-destructive"
            >
              {error}
            </p>
          )}
        </form>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={close}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <LoadingButton
            type="button"
            onClick={save}
            loading={mutation.isPending}
            disabled={!dirty || validationError !== null}
          >
            Save
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
