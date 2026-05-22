import { useCallback, useEffect, useRef, useState } from "react"

import { LoadingButton } from "@/components/ui/loading-button"
import { cn } from "@/lib/utils"

export type TotpFormData = { code: string }

interface TotpFormProps {
  onSubmit: (data: TotpFormData) => void
  loading?: boolean
  autoSubmit?: boolean
  buttonLabel?: string
  /** Pass `null` to hide the visible label (we fall back to an aria-label). */
  label?: string | null
  /** When true, all slots render with a destructive border + helper line. */
  invalid?: boolean
  /** Overrides the default "That code didn't match. Try again." helper. */
  errorMessage?: string | null
  /** Fired on every digit edit. Parents typically clear `invalid` here. */
  onCodeChange?: () => void
  /**
   * When true, an `invalid` rising edge starts a ~3s "wave" through the
   * slots: forward sweep shakes each slot, apex/backward sweep shakes
   * and clears each digit, then focus returns to slot 0. Any pointer or
   * keyboard interaction with a slot cancels mid-flight. Suppresses the
   * row shake in this mode — the wave is the reaction.
   */
  autoClearOnInvalid?: boolean
}

const SLOT_COUNT = 6
// 5 forward (shake only) + 1 apex (shake + clear) + 5 backward (shake + clear)
const CLEANUP_STEPS = SLOT_COUNT * 2 - 1
const CLEANUP_TICK_MS = 270

/**
 * 6-slot numeric input for TOTP codes. Each slot accepts one digit;
 * typing auto-advances to the next slot, backspacing on an empty slot
 * jumps back and clears the previous one, and pasting a 6-digit code
 * anywhere auto-fills all slots. Optional auto-submit when the code
 * reaches 6 digits, with a latch so a server error that flips
 * `loading: true → false` doesn't re-fire the same code.
 */
export function TotpForm({
  onSubmit,
  loading = false,
  autoSubmit = true,
  buttonLabel = "Verify",
  label = "Authentication code",
  invalid = false,
  errorMessage,
  onCodeChange,
  autoClearOnInvalid = false,
}: TotpFormProps) {
  const [digits, setDigits] = useState<string[]>(() =>
    Array(SLOT_COUNT).fill(""),
  )
  const inputRefs = useRef<(HTMLInputElement | null)[]>([])
  const lastSubmittedCode = useRef<string | null>(null)
  const [shaking, setShaking] = useState(false)
  const [shakingSlot, setShakingSlot] = useState<number | null>(null)
  const prevInvalid = useRef(false)
  const cleanupTimeoutRef = useRef<number | null>(null)

  const code = digits.join("")
  const complete = code.length === SLOT_COUNT

  const focusSlot = (i: number) => {
    const el = inputRefs.current[i]
    if (el) {
      el.focus()
      el.select()
    }
  }

  const notifyChange = () => {
    onCodeChange?.()
  }

  const writeDigits = (next: string[]) => {
    setDigits(next)
    notifyChange()
  }

  // Focus the first slot on mount so the user can start typing
  // immediately when the form appears (login MFA tab, enroll dialog).
  useEffect(() => {
    focusSlot(0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const stopCleanup = useCallback(() => {
    if (cleanupTimeoutRef.current !== null) {
      window.clearTimeout(cleanupTimeoutRef.current)
      cleanupTimeoutRef.current = null
    }
    setShakingSlot(null)
  }, [])

  // Run one step of the wave, then schedule the next. Adjacent steps
  // always touch different slot indices, so swapping `shakingSlot` is
  // enough to re-trigger the CSS animation each time.
  const advanceCleanup = useCallback(
    (step: number) => {
      if (step >= CLEANUP_STEPS) {
        stopCleanup()
        focusSlot(0)
        // Tell the parent the code is gone so it can drop `invalid` and
        // the red highlight clears in sync with the wave finishing.
        onCodeChange?.()
        return
      }
      // step 0..4 → forward sweep (slot = step)
      // step 5    → apex on slot 5 (shake + clear)
      // step 6..10 → backward sweep, shake + clear (slot = 10 - step)
      const isForwardOnly = step < SLOT_COUNT - 1
      const slotIdx = isForwardOnly ? step : CLEANUP_STEPS - 1 - step
      setShakingSlot(slotIdx)
      if (!isForwardOnly) {
        setDigits((prev) => {
          if (prev[slotIdx] === "") return prev
          const next = [...prev]
          next[slotIdx] = ""
          return next
        })
      }
      cleanupTimeoutRef.current = window.setTimeout(
        () => advanceCleanup(step + 1),
        CLEANUP_TICK_MS,
      )
    },
    [stopCleanup, onCodeChange],
  )

  const startCleanup = useCallback(() => {
    stopCleanup()
    advanceCleanup(0)
  }, [advanceCleanup, stopCleanup])

  // Trigger the reaction whenever `invalid` flips false → true. In
  // auto-clear mode we run the wave; otherwise we fire the row shake.
  useEffect(() => {
    if (invalid && !prevInvalid.current) {
      if (autoClearOnInvalid) {
        startCleanup()
      } else {
        setShaking(false)
        // Re-arm on the next frame so the class re-applies cleanly.
        requestAnimationFrame(() => setShaking(true))
      }
    }
    prevInvalid.current = invalid
  }, [invalid, autoClearOnInvalid, startCleanup])

  // Clear any pending cleanup tick on unmount.
  useEffect(() => () => stopCleanup(), [stopCleanup])

  // Any user touch on the slots during the wave aborts the sequence.
  // Whatever digits were already cleared stay cleared.
  const cancelCleanup = () => {
    if (cleanupTimeoutRef.current !== null) stopCleanup()
  }

  // Auto-submit when complete, with a per-code latch so retries after a
  // server error (loading flipping back to false) don't replay the same
  // value. The latch clears once the user backspaces below 6 digits.
  useEffect(() => {
    if (!autoSubmit) return
    if (loading) return
    if (!complete) {
      if (lastSubmittedCode.current !== null) {
        lastSubmittedCode.current = null
      }
      return
    }
    if (lastSubmittedCode.current === code) return
    lastSubmittedCode.current = code
    onSubmit({ code })
  }, [autoSubmit, code, complete, loading, onSubmit])

  const handleChange = (i: number, raw: string) => {
    cancelCleanup()
    const cleaned = raw.replace(/[^0-9]/g, "")
    if (cleaned.length === 0) {
      const next = [...digits]
      next[i] = ""
      writeDigits(next)
      return
    }
    // Multi-char input (e.g., IME / autofill) — distribute across slots
    // starting from the current one.
    if (cleaned.length > 1) {
      const next = [...digits]
      let cursor = i
      for (const ch of cleaned) {
        if (cursor >= SLOT_COUNT) break
        next[cursor] = ch
        cursor++
      }
      writeDigits(next)
      focusSlot(Math.min(cursor, SLOT_COUNT - 1))
      return
    }
    const next = [...digits]
    next[i] = cleaned
    writeDigits(next)
    if (i < SLOT_COUNT - 1) focusSlot(i + 1)
  }

  const handleKeyDown = (
    i: number,
    e: React.KeyboardEvent<HTMLInputElement>,
  ) => {
    cancelCleanup()
    if (e.key === "Backspace") {
      if (digits[i]) {
        // Browser clears the current value; we still need to fire the
        // change callback so parents know the user is editing.
        const next = [...digits]
        next[i] = ""
        writeDigits(next)
        e.preventDefault()
        return
      }
      e.preventDefault()
      if (i > 0) {
        const next = [...digits]
        next[i - 1] = ""
        writeDigits(next)
        focusSlot(i - 1)
      }
    } else if (e.key === "ArrowLeft") {
      e.preventDefault()
      if (i > 0) focusSlot(i - 1)
    } else if (e.key === "ArrowRight") {
      e.preventDefault()
      if (i < SLOT_COUNT - 1) focusSlot(i + 1)
    }
  }

  const handlePaste = (
    i: number,
    e: React.ClipboardEvent<HTMLInputElement>,
  ) => {
    cancelCleanup()
    const text = e.clipboardData.getData("text")
    const digitsOnly = text.replace(/[^0-9]/g, "")
    if (!digitsOnly) return
    e.preventDefault()
    const next = [...digits]
    // A full-length paste fills from slot 0; a partial paste fills from
    // the focused slot so a user can paste into the middle if they want.
    let cursor = digitsOnly.length >= SLOT_COUNT ? 0 : i
    for (const ch of digitsOnly) {
      if (cursor >= SLOT_COUNT) break
      next[cursor] = ch
      cursor++
    }
    writeDigits(next)
    focusSlot(Math.min(cursor, SLOT_COUNT - 1))
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!complete) {
      const firstEmpty = digits.findIndex((d) => !d)
      focusSlot(firstEmpty >= 0 ? firstEmpty : 0)
      return
    }
    onSubmit({ code })
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      <div className="space-y-2">
        {label !== null && (
          <div
            className="text-sm font-medium text-center"
            id="totp-form-label"
          >
            {label}
          </div>
        )}
        <div
          className={cn(
            "flex items-center justify-center gap-2",
            shaking && "animate-shake-x",
          )}
          role="group"
          aria-labelledby={label !== null ? "totp-form-label" : undefined}
          aria-label={label === null ? "Authentication code" : undefined}
          onAnimationEnd={() => setShaking(false)}
        >
          {digits.map((d, i) => (
            <input
              key={i}
              ref={(el) => {
                inputRefs.current[i] = el
              }}
              type="text"
              inputMode="numeric"
              pattern="[0-9]*"
              autoComplete={i === 0 ? "one-time-code" : "off"}
              maxLength={1}
              value={d}
              onChange={(e) => handleChange(i, e.target.value)}
              onKeyDown={(e) => handleKeyDown(i, e)}
              onPaste={(e) => handlePaste(i, e)}
              onFocus={(e) => e.target.select()}
              onMouseDown={cancelCleanup}
              disabled={loading}
              aria-label={`Digit ${i + 1} of ${SLOT_COUNT}`}
              aria-invalid={invalid || undefined}
              className={cn(
                "h-12 w-10 rounded-md border text-center font-mono text-lg",
                "focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-1",
                "disabled:cursor-not-allowed disabled:opacity-60",
                invalid
                  ? "border-destructive bg-destructive/5 text-destructive focus:ring-destructive"
                  : "border-input bg-background",
                shakingSlot === i && "animate-shake-slot",
              )}
            />
          ))}
        </div>
        {invalid && (
          <p className="text-center text-xs text-destructive" role="alert">
            {errorMessage ?? "That code didn't match. Try again."}
          </p>
        )}
      </div>
      <LoadingButton type="submit" loading={loading} disabled={!complete}>
        {buttonLabel}
      </LoadingButton>
    </form>
  )
}
