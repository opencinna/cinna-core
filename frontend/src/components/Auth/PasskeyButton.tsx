import { Fingerprint, Loader2 } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { isWebAuthnSupported } from "@/utils/webauthn"

interface PasskeyButtonProps {
  onClick: () => void
  loading?: boolean
  disabled?: boolean
}

/**
 * The "Use my passkey" button shown on the MFA challenge page. Greyed
 * out with an explanatory tooltip on browsers without WebAuthn support.
 */
export function PasskeyButton({
  onClick,
  loading = false,
  disabled = false,
}: PasskeyButtonProps) {
  const supported = isWebAuthnSupported()
  const isDisabled = !supported || loading || disabled

  const button = (
    <Button
      type="button"
      variant="default"
      className="w-full"
      onClick={onClick}
      disabled={isDisabled}
      aria-label="Sign in with passkey"
    >
      {loading ? (
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
      ) : (
        <Fingerprint className="mr-2 h-4 w-4" />
      )}
      Use my passkey
    </Button>
  )

  if (supported) {
    return button
  }

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <span>{button}</span>
        </TooltipTrigger>
        <TooltipContent side="top" className="max-w-xs text-xs">
          Your browser doesn't support passkeys; try Chrome, Safari, Firefox, or
          Edge.
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
