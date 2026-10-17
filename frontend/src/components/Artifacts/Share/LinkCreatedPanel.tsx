import { useEffect, useRef } from "react"

import type { ArtifactLinkCreated } from "@/client"
import { CopyableValue } from "@/components/Common/CopyableValue"
import { Button } from "@/components/ui/button"
import { DialogFooter } from "@/components/ui/dialog"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useCopyToClipboard } from "@/hooks/useCopyToClipboard"
import useCustomToast from "@/hooks/useCustomToast"

interface LinkCreatedPanelProps {
  link: ArtifactLinkCreated
  onDone: () => void
}

/**
 * The one-time result of creating a link: the URL (hashed at rest, so never
 * shown again) and, when coded, the code as one click-to-copy control.
 */
export function LinkCreatedPanel({ link, onDone }: LinkCreatedPanelProps) {
  const [, copy] = useCopyToClipboard()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const bodyRef = useRef<HTMLDivElement>(null)

  // The Create button that led here unmounted; focus the panel rather than
  // the first control, whose tooltip would open over nothing.
  useEffect(() => {
    bodyRef.current?.focus()
  }, [])

  const copyCode = async () => {
    if (!link.code) return
    if (await copy(link.code)) showSuccessToast("Code copied")
    else showErrorToast("Couldn't copy the code")
  }

  return (
    <>
      <div ref={bodyRef} tabIndex={-1} className="space-y-4 outline-none">
        <p className="text-sm">Copy this link now — it can't be shown again.</p>
        <CopyableValue label="Link" value={link.url} />
        {link.code && (
          <div className="space-y-1.5">
            <p className="text-sm font-medium">Security code</p>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  className="flex gap-2 rounded-md outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
                  aria-label={`Copy the security code ${link.code}`}
                  onClick={() => void copyCode()}
                >
                  {link.code.split("").map((digit, i) => (
                    <span
                      key={i}
                      className="flex h-12 w-10 items-center justify-center rounded-md border bg-muted/50 font-mono text-xl font-semibold"
                    >
                      {digit}
                    </span>
                  ))}
                </button>
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                Click to copy
              </TooltipContent>
            </Tooltip>
            <p className="text-xs text-muted-foreground">
              Send the code separately. Three wrong codes block the link.
            </p>
          </div>
        )}
      </div>
      <DialogFooter>
        <Button type="button" onClick={onDone}>
          Done
        </Button>
      </DialogFooter>
    </>
  )
}
