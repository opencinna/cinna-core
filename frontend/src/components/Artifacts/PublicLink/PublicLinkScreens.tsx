import { AlertCircle, Download, Info, Loader2, Shapes } from "lucide-react"
import {
  type ClipboardEvent,
  type KeyboardEvent,
  useRef,
  useState,
} from "react"

import { Button } from "@/components/ui/button"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"

/** The `GuestLoadingScreen` shape: a full-page wait, not a card. */
export function PublicLinkLoading() {
  return (
    <div className="flex h-dvh flex-col items-center justify-center bg-background">
      <div className="flex flex-col items-center gap-4">
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-primary/10">
          <Shapes className="h-8 w-8 text-primary" />
        </div>
        <div className="flex items-center gap-2">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          <p className="text-muted-foreground">Opening…</p>
        </div>
      </div>
    </div>
  )
}

/** The `GuestErrorScreen` shape, for expired / blocked / gone links. */
export function PublicLinkError({ message }: { message: string }) {
  return (
    <div className="flex h-dvh flex-col items-center justify-center bg-background">
      <div className="flex max-w-md flex-col items-center gap-4 px-4 text-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-destructive/10">
          <AlertCircle className="h-8 w-8 text-destructive" />
        </div>
        <h1 className="text-xl font-semibold">Unable to open</h1>
        <p className="text-muted-foreground">{message}</p>
      </div>
    </div>
  )
}

/** The `GuestChatHeader` shape: what this is and who sent it, and Download. */
export function PublicLinkBar({
  title,
  byline,
  downloadHref,
  description,
}: {
  title: string
  byline: string
  downloadHref: string | null
  /** The owner's description; an `Info` icon that opens it in a popover (tap works on phones). */
  description: string
}) {
  return (
    <header className="flex h-14 shrink-0 items-center gap-4 border-b bg-background px-4">
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/10">
          <Shapes className="h-4 w-4 text-primary" />
        </div>
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold">{title}</h1>
          {byline && (
            <p className="truncate text-xs text-muted-foreground">{byline}</p>
          )}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {downloadHref && (
          <Button variant="outline" size="sm" className="shrink-0" asChild>
            <a href={downloadHref}>
              <Download className="h-4 w-4" />
              Download
            </a>
          </Button>
        )}
        {description && (
          <Popover>
            <PopoverTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8 shrink-0"
                aria-label="About this artifact"
              >
                <Info className="h-4 w-4" />
              </Button>
            </PopoverTrigger>
            <PopoverContent
              side="bottom"
              align="end"
              className="max-h-[60vh] w-80 max-w-[calc(100vw-2rem)] overflow-y-auto whitespace-pre-wrap break-words text-sm"
            >
              {description}
            </PopoverContent>
          </Popover>
        )}
      </div>
    </header>
  )
}

/**
 * The `SecurityCodeScreen` shape: four digit boxes with paste, Continue, and
 * the server's attempts-left sentence under the boxes.
 */
export function PublicLinkCodeScreen({
  title,
  error,
  onSubmit,
}: {
  title: string
  /** "Incorrect code. 2 attempts left." after a wrong try. */
  error: string | null
  onSubmit: (code: string) => Promise<void>
}) {
  const [digits, setDigits] = useState(["", "", "", ""])
  const [submitting, setSubmitting] = useState(false)
  const inputs = useRef<(HTMLInputElement | null)[]>([])

  const submit = async (code: string) => {
    if (code.length !== 4 || submitting) return
    setSubmitting(true)
    try {
      await onSubmit(code)
    } finally {
      setSubmitting(false)
      setDigits(["", "", "", ""])
      inputs.current[0]?.focus()
    }
  }

  const setDigit = (index: number, value: string) => {
    if (value && !/^\d$/.test(value)) return
    const next = [...digits]
    next[index] = value
    setDigits(next)
    if (value && index < 3) inputs.current[index + 1]?.focus()
  }

  const onKeyDown = (index: number, e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Backspace" && !digits[index] && index > 0) {
      inputs.current[index - 1]?.focus()
    }
    if (e.key === "Enter") void submit(digits.join(""))
  }

  const onPaste = (e: ClipboardEvent) => {
    e.preventDefault()
    const text = e.clipboardData.getData("text").replace(/\D/g, "").slice(0, 4)
    if (!text) return
    const next = ["", "", "", ""]
    for (let i = 0; i < text.length; i++) next[i] = text[i]
    setDigits(next)
    inputs.current[Math.min(text.length, 3)]?.focus()
  }

  const filled = digits.every((d) => d !== "")

  return (
    <div className="flex h-dvh flex-col items-center justify-center bg-background">
      <form
        className="flex w-full max-w-sm flex-col items-center gap-6 px-4"
        onSubmit={(e) => {
          e.preventDefault()
          void submit(digits.join(""))
        }}
      >
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-primary/10">
          <Shapes className="h-8 w-8 text-primary" />
        </div>
        <div className="text-center">
          <h1 className="text-xl font-semibold break-words">{title}</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Enter the 4-digit code you were given.
          </p>
        </div>
        <fieldset
          className="flex gap-3"
          onPaste={onPaste}
          aria-label="Security code"
        >
          {digits.map((digit, i) => (
            <input
              key={i}
              ref={(el) => {
                inputs.current[i] = el
              }}
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={1}
              value={digit}
              aria-label={`Digit ${i + 1}`}
              onChange={(e) => setDigit(i, e.target.value)}
              onKeyDown={(e) => onKeyDown(i, e)}
              // biome-ignore lint/a11y/noAutofocus: the code is the page's only task
              autoFocus={i === 0}
              disabled={submitting}
              className="h-16 w-14 rounded-lg border bg-background text-center font-mono text-2xl font-bold focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary"
            />
          ))}
        </fieldset>
        {error && (
          <p className="text-center text-sm text-destructive" role="alert">
            {error}
          </p>
        )}
        <Button
          type="submit"
          size="lg"
          className="w-full"
          disabled={!filled || submitting}
        >
          {submitting ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin" />
              Checking…
            </>
          ) : (
            "Continue"
          )}
        </Button>
      </form>
    </div>
  )
}
