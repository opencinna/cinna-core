import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { MarkdownRenderer } from "@/components/Chat/MarkdownRenderer"
import { ShieldCheck } from "lucide-react"

interface DisclaimerModalProps {
  open: boolean
  markdown: string
  onAcknowledge: () => void
}

/**
 * Non-dismissible disclaimer modal shown at login before onboarding.
 *
 * The modal can only be closed via the "I Understand" button — outside-click
 * and escape are ignored (we don't act on overlay-driven `onOpenChange`).
 */
export function DisclaimerModal({
  open,
  markdown,
  onAcknowledge,
}: DisclaimerModalProps) {
  return (
    <Dialog open={open}>
      <DialogContent
        className="sm:max-w-[640px] max-h-[80vh] p-0 overflow-hidden gap-0"
        // Block all the built-in dismissal paths — the user must acknowledge.
        onPointerDownOutside={(e) => e.preventDefault()}
        onInteractOutside={(e) => e.preventDefault()}
        onEscapeKeyDown={(e) => e.preventDefault()}
        showCloseButton={false}
      >
        <DialogHeader className="px-6 pt-6 pb-4 border-b">
          <DialogTitle className="flex items-center gap-2 text-base">
            <ShieldCheck className="h-5 w-5 text-violet-500" />
            Please Read
          </DialogTitle>
        </DialogHeader>

        <div className="px-6 py-5 overflow-y-auto max-h-[55vh] prose prose-sm dark:prose-invert max-w-none">
          <MarkdownRenderer
            content={markdown}
            className="[&>h1:first-child]:!mt-0 [&>h2:first-child]:!mt-0 [&>h3:first-child]:!mt-0 [&>h4:first-child]:!mt-0"
          />
        </div>

        <div className="px-6 py-4 border-t flex justify-end">
          <Button onClick={onAcknowledge}>I Understand</Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
