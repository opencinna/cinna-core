import { Check, Copy, Download, Printer } from "lucide-react"
import { useState } from "react"

import type { RecoveryCodesPlaintext } from "@/client"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import useCustomToast from "@/hooks/useCustomToast"

interface RecoveryCodesDialogProps {
  open: boolean
  codes: RecoveryCodesPlaintext | null
  /** Title override — defaults match the enrollment one-shot context. */
  title?: string
  description?: string
  /** Called when the user explicitly confirms "I've saved these codes". */
  onConfirm: () => void
}

/**
 * One-shot plaintext display of recovery codes with copy / download /
 * print actions. The dialog cannot be dismissed without explicit
 * confirmation — closing it any other way would discard the codes
 * forever, so we hide the close button and ignore overlay clicks.
 */
export function RecoveryCodesDialog({
  open,
  codes,
  title = "Save your recovery codes",
  description = "Store these in a password manager or a printed copy in a safe place. Each code can be used exactly once to sign in if you lose your other factors.",
  onConfirm,
}: RecoveryCodesDialogProps) {
  const { showSuccessToast } = useCustomToast()
  const [confirmed, setConfirmed] = useState(false)
  const [allCopied, setAllCopied] = useState(false)
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null)

  const handleCopyOne = async (code: string, index: number) => {
    await navigator.clipboard.writeText(code)
    setCopiedIndex(index)
    setTimeout(() => setCopiedIndex(null), 1500)
  }

  const handleCopyAll = async () => {
    if (!codes) return
    await navigator.clipboard.writeText(codes.codes.join("\n"))
    setAllCopied(true)
    showSuccessToast("Recovery codes copied to clipboard")
    setTimeout(() => setAllCopied(false), 1500)
  }

  const handleDownload = () => {
    if (!codes) return
    const blob = new Blob([codes.codes.join("\n")], { type: "text/plain" })
    const url = URL.createObjectURL(blob)
    const link = document.createElement("a")
    link.href = url
    link.download = "cinna-recovery-codes.txt"
    link.click()
    URL.revokeObjectURL(url)
  }

  const handlePrint = () => {
    if (!codes) return
    const printable = codes.codes.join("\n")
    const win = window.open("", "_blank", "noopener,noreferrer")
    if (!win) return
    // Build the printable view via the DOM so the codes are treated as
    // text and never parsed as HTML — avoids any chance of a malicious /
    // surprising character breaking out of the markup.
    win.document.title = "Recovery codes"
    const pre = win.document.createElement("pre")
    pre.style.cssText =
      "font-family: monospace; font-size: 1.1rem; line-height: 1.5; padding: 24px;"
    pre.textContent = printable
    win.document.body.appendChild(pre)
    win.print()
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Never allow casual dismissal — only the explicit confirm flow.
        if (!next && !confirmed) return
        if (!next && confirmed) onConfirm()
      }}
    >
      <DialogContent
        showCloseButton={false}
        className="sm:max-w-lg"
        onInteractOutside={(e) => e.preventDefault()}
        onEscapeKeyDown={(e) => e.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        <div className="my-4 grid grid-cols-2 gap-2">
          {codes?.codes.map((code, index) => (
            <button
              key={code}
              type="button"
              onClick={() => handleCopyOne(code, index)}
              className="group flex items-center justify-between gap-2 rounded-md border bg-muted/40 px-3 py-2 font-mono text-sm hover:bg-muted"
              aria-label={`Copy recovery code ${index + 1}`}
            >
              <span>{code}</span>
              {copiedIndex === index ? (
                <Check className="h-3.5 w-3.5 text-emerald-500" />
              ) : (
                <Copy className="h-3.5 w-3.5 opacity-50 group-hover:opacity-100" />
              )}
            </button>
          ))}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={handleCopyAll}
          >
            {allCopied ? (
              <Check className="mr-2 h-4 w-4" />
            ) : (
              <Copy className="mr-2 h-4 w-4" />
            )}
            Copy all
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={handleDownload}
          >
            <Download className="mr-2 h-4 w-4" />
            Download .txt
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={handlePrint}
          >
            <Printer className="mr-2 h-4 w-4" />
            Print
          </Button>
        </div>

        <div className="mt-4 flex items-start gap-2">
          <Checkbox
            id="confirm-recovery"
            checked={confirmed}
            onCheckedChange={(checked) => setConfirmed(checked === true)}
          />
          <Label htmlFor="confirm-recovery" className="cursor-pointer">
            I've saved these codes somewhere safe
          </Label>
        </div>

        <DialogFooter>
          <Button type="button" disabled={!confirmed} onClick={onConfirm}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
