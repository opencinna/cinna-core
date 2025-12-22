import { EllipsisVertical } from "lucide-react"
import { useState } from "react"

import type { CredentialPublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import DeleteCredential from "../Credentials/DeleteCredential"
import EditCredential from "../Credentials/EditCredential"

interface CredentialActionsMenuProps {
  credential: CredentialPublic
}

export const CredentialActionsMenu = ({ credential }: CredentialActionsMenuProps) => {
  const [open, setOpen] = useState(false)

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon">
          <EllipsisVertical />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <EditCredential credential={credential} onSuccess={() => setOpen(false)} />
        <DeleteCredential credential={credential} onSuccess={() => setOpen(false)} />
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
