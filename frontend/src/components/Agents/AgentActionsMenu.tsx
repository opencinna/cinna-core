import { EllipsisVertical } from "lucide-react"
import { useState } from "react"

import type { AgentPublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import DeleteAgent from "../Agents/DeleteAgent"
import EditAgent from "../Agents/EditAgent"

interface AgentActionsMenuProps {
  agent: AgentPublic
}

export const AgentActionsMenu = ({ agent }: AgentActionsMenuProps) => {
  const [open, setOpen] = useState(false)

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon">
          <EllipsisVertical />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <EditAgent agent={agent} onSuccess={() => setOpen(false)} />
        <DeleteAgent id={agent.id} onSuccess={() => setOpen(false)} />
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
