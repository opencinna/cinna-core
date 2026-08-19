import { Sparkles } from "lucide-react"
import { useState } from "react"

import type { SessionPublicExtended } from "@/client"
import { DropdownMenuItem } from "@/components/ui/dropdown-menu"
import { ImproveAgentModal } from "./ImproveAgentModal"

interface ImproveAgentMenuItemProps {
  session: SessionPublicExtended
  /** Called once a request has been submitted, so the caller can close its menu. */
  onSuccess: () => void
}

/**
 * Session options-menu entry that opens the improvement-request consent modal.
 * Owns its own dialog state, exactly like `EditSession` / `DeleteSession`, so
 * the dropdown does not have to know the modal exists.
 */
const ImproveAgentMenuItem = ({
  session,
  onSuccess,
}: ImproveAgentMenuItemProps) => {
  const [isOpen, setIsOpen] = useState(false)

  return (
    <>
      <DropdownMenuItem
        onSelect={(e) => e.preventDefault()}
        onClick={() => setIsOpen(true)}
      >
        <Sparkles />
        Improve Agent
      </DropdownMenuItem>
      <ImproveAgentModal
        sessionId={session.id}
        open={isOpen}
        onOpenChange={setIsOpen}
        onSubmitted={onSuccess}
      />
    </>
  )
}

export default ImproveAgentMenuItem
