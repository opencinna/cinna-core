import { Check, Copy } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import useCustomToast from "@/hooks/useCustomToast"

/**
 * Read-only value with a copy button.
 *
 * For values that exist to be pasted somewhere else verbatim — a webhook URL
 * into a Google console field, the `/start` URL into an internal wiki. Copying
 * beats selecting once the string is long enough to mis-select.
 *
 * Lives here rather than beside its first caller because it now has two, and a
 * second hand-rolled copy control is how the tick timing, the aria-label and
 * the failure toast drift apart. `navigator.clipboard` is undefined outside a
 * secure context and `writeText` rejects when permission is denied; unhandled,
 * both leave a button that visibly does nothing, so the catch is not optional.
 *
 * One deliberate exception: `Desktop/DesktopDownloadSection` keeps its own
 * inline control. It is a different affordance — a full-width bordered address
 * block with a ghost icon button and a *success* toast, on a page an anonymous
 * visitor reads — and folding it in here would mean parameterising layout,
 * button variant and toast behaviour until this component described nothing.
 * Any *new* label-plus-value control should use this one.
 */
export function CopyableValue({
  label,
  value,
}: {
  label: string
  value: string
}) {
  const [copied, setCopied] = useState(false)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const { showErrorToast } = useCustomToast()

  useEffect(
    () => () => {
      if (resetTimer.current) clearTimeout(resetTimer.current)
    },
    [],
  )

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      resetTimer.current = setTimeout(() => setCopied(false), 2000)
    } catch {
      showErrorToast(`Failed to copy ${label.toLowerCase()}`)
    }
  }

  return (
    <div className="space-y-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <div className="flex items-center gap-2">
        <code className="flex-1 rounded border bg-background px-2 py-1.5 font-mono text-xs break-all">
          {value}
        </code>
        <Button
          variant="outline"
          size="icon"
          className="h-8 w-8 shrink-0"
          onClick={copy}
          aria-label={`Copy ${label}`}
        >
          {copied ? (
            <Check className="h-3 w-3 text-green-500" />
          ) : (
            <Copy className="h-3 w-3" />
          )}
        </Button>
      </div>
    </div>
  )
}
