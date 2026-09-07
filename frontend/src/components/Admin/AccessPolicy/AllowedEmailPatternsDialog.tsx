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
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import { Textarea } from "@/components/ui/textarea"
import {
  describePatternCount,
  parsePatterns,
  useServerConfigUpdate,
} from "./accessPolicy"

/**
 * The glob list that gates self-registration, edited as a form.
 *
 * A dialog rather than a field on the Registration card: it is the only thing
 * on that concern with a Save and a Cancel, and an explicit form beside
 * auto-saving controls is two interaction models in one card.
 *
 * Nothing here is validated client-side. `access_policy.md` makes matching a
 * server-side question on purpose — a second matcher in the browser is a
 * second answer that disagrees the first time either changes — so a rejected
 * list comes back from the server and is rendered under the field that still
 * holds the text that caused it.
 */
export function AllowedEmailPatternsDialog({
  open,
  onOpenChange,
  persisted,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The saved list. `null` means "showing the persisted value" locally. */
  persisted: string
}) {
  const [draft, setDraft] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const value = draft ?? persisted
  const dirty = value !== persisted
  const count = parsePatterns(value).length

  const close = () => {
    setDraft(null)
    setError(null)
    onOpenChange(false)
  }

  const mutation = useServerConfigUpdate({
    onSaved: close,
    onError: setError,
  })

  const save = () => {
    setError(null)
    // Never `null`: the backend reads a null field as "not being changed", so
    // clearing the list has to travel as an empty string.
    mutation.mutate({ allowed_email_patterns: value.trim() })
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => (next ? onOpenChange(true) : close())}
    >
      {/* Escape and an outside click are the two ways out that ignore a
          disabled Cancel. Closed mid-save, the failure would be written into
          state nobody is looking at — the override replaces the toast rather
          than supplementing it — and the admin would watch the dialog vanish
          and conclude the list saved. */}
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
          <DialogTitle>Allowed email addresses</DialogTitle>
          <DialogDescription>
            Comma-separated globs. Empty means no restriction.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-2 py-2">
          <div className="flex items-center justify-between gap-4">
            <Label
              htmlFor="allowed-email-patterns"
              className="text-sm font-medium"
            >
              Allowed email addresses
            </Label>
            {/* The draft the admin is typing, not the saved policy — the card
                behind this dialog is what describes what is actually in
                force. */}
            <span className="shrink-0 text-xs text-muted-foreground">
              {describePatternCount(count)}
            </span>
          </div>
          <Textarea
            id="allowed-email-patterns"
            value={value}
            onChange={(event) => {
              setDraft(event.target.value)
              // The error and `aria-invalid` describe the text that was
              // rejected; once that text changes they describe nothing.
              if (error) setError(null)
            }}
            placeholder="*@acme.com, *@*.acme.com"
            className="min-h-[80px] font-mono text-sm"
            aria-invalid={error ? true : undefined}
            aria-describedby={
              error
                ? "allowed-email-patterns-error allowed-email-patterns-help"
                : "allowed-email-patterns-help"
            }
          />
          {error && (
            <p
              id="allowed-email-patterns-error"
              role="alert"
              className="text-xs text-destructive"
            >
              {error}
            </p>
          )}
          <p
            id="allowed-email-patterns-help"
            className="text-xs text-muted-foreground"
          >
            For example <code>*@acme.com</code>, <code>*@*.acme.com</code>, or{" "}
            <code>jane@acme.com</code>. <code>*</code> matches every address and
            means no restriction, which is also what an empty list means.
            Patterns gate registration only — editing them never locks out an
            existing user.
          </p>
        </div>

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
            disabled={!dirty}
          >
            Save
          </LoadingButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
