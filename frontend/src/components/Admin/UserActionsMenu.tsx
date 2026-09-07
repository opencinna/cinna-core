import { EllipsisVertical } from "lucide-react"
import { useState } from "react"

import type { UserPublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import useAuth from "@/hooks/useAuth"
import DeleteUser from "./DeleteUser"
import EditUser from "./EditUser"
import {
  canRevokeInvitation,
  InvitationMenuItems,
  RevokeInvitationItem,
} from "./InvitationActions"

interface UserActionsMenuProps {
  user: UserPublic
}

/**
 * Everything an admin can do to one row, in one menu.
 *
 * Order is the pattern's: secondary actions, one separator, then the
 * destructive ones. Revoke used to sit mid-menu, ahead of Edit, which gave the
 * menu two destructive groups and put "kill this invitation" one row above
 * "edit this person".
 */
export const UserActionsMenu = ({ user }: UserActionsMenuProps) => {
  const [open, setOpen] = useState(false)
  const { user: currentUser } = useAuth()

  const isSelf = user.id === currentUser?.id
  const showDelete = !isSelf
  const showRevoke = canRevokeInvitation(user)

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon">
          <EllipsisVertical />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {/* Renders nothing unless this account has an outstanding invitation.
            Placed first: for an invited account the invitation actions are the
            reason the admin opened this menu. */}
        <InvitationMenuItems user={user} onSuccess={() => setOpen(false)} />
        <EditUser user={user} onSuccess={() => setOpen(false)} />
        {(showRevoke || showDelete) && <DropdownMenuSeparator />}
        <RevokeInvitationItem user={user} onSuccess={() => setOpen(false)} />
        {showDelete && (
          <DeleteUser id={user.id} onSuccess={() => setOpen(false)} />
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
