import { CheckCircle2 } from "lucide-react"

import { Button } from "@/components/ui/button"
import { DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"

/** A key issued to someone else: the body swaps to this, no navigation. */
export interface IssuedToOther {
  recipientName: string
  keyLabel: string
  producerName: string
}

/**
 * The Give-access dialog's success body when the key went to someone else:
 * the issuer never sees its value, so there is nowhere to navigate to.
 */
export function AgentApiKeyIssuedPanel({
  recipientName,
  keyLabel,
  producerName,
  onDone,
}: IssuedToOther & { onDone: () => void }) {
  return (
    <>
      <DialogHeader>
        <DialogTitle>Give API access</DialogTitle>
      </DialogHeader>
      <div className="flex items-start gap-3">
        <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-success" />
        <div className="min-w-0 space-y-1">
          <p className="text-sm font-medium break-words">
            {recipientName} has a new key
          </p>
          <p className="text-sm text-muted-foreground break-words">
            It is in their credentials as {keyLabel}. You won't see its value.
            Revoke it from {producerName}'s API access card, or from Credentials
            › Issued Credentials.
          </p>
        </div>
      </div>
      <DialogFooter>
        <Button onClick={onDone}>Done</Button>
      </DialogFooter>
    </>
  )
}
