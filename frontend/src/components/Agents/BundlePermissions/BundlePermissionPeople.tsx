import { Pencil, Trash2 } from "lucide-react"
import { Fragment, useState } from "react"

import type { BundlePermissionProducer, BundlePermissionUser } from "@/client"
import { existingGrantFor } from "@/components/Agents/BundlePermissions/bundlePermissionsActions"
import { ProducerScopeInline } from "@/components/Agents/BundlePermissions/ProducerScopeInline"
import { ListRow, ListRowGroup } from "@/components/Common/ListRow"
import { PreviewList } from "@/components/Common/PreviewList"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { pluralize } from "@/utils/pluralize"

interface BundlePermissionUserRowProps {
  user: BundlePermissionUser
  manageableProducers: BundlePermissionProducer[]
  onEdit: (user: BundlePermissionUser) => void
  onRemove: (user: BundlePermissionUser) => void
}

function BundlePermissionUserRow({
  user,
  manageableProducers,
  onEdit,
  onRemove,
}: BundlePermissionUserRowProps) {
  return (
    <ListRow
      title={user.full_name || user.email || user.user_id}
      // Membership in this list *is* bundle access, so the row has no on/off
      // state and therefore no leading dot: a dot that is green on every row
      // of every list is decoration.
      meta={user.full_name && user.email ? user.email : undefined}
      // The scope clusters are this row's passive read: what the person may
      // do on each connected producer. They are also the shortcut into the
      // editor, which is why they sit left of the menu rather than in it.
      flags={manageableProducers.map((producer) => (
        <ProducerScopeInline
          key={producer.producer_agent_id}
          producer={producer}
          grant={existingGrantFor(producer, user.user_id)}
          onEdit={() => onEdit(user)}
        />
      ))}
    >
      <RowActionsMenu
        label={`${user.full_name || user.email || "this user"}'s permissions`}
      >
        <DropdownMenuItem onSelect={() => onEdit(user)}>
          <Pencil />
          Edit permissions
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          variant="destructive"
          onSelect={(e) => {
            e.preventDefault()
            onRemove(user)
          }}
        >
          <Trash2 />
          Remove access
        </DropdownMenuItem>
      </RowActionsMenu>
    </ListRow>
  )
}

interface BundlePermissionPeopleProps {
  /** Never empty — the card skips the section. */
  users: BundlePermissionUser[]
  manageableProducers: BundlePermissionProducer[]
  /** Name the section only when a Groups section sits under it. */
  showHeading: boolean
  onEdit: (user: BundlePermissionUser) => void
  onRemove: (user: BundlePermissionUser) => void
}

/**
 * The card's "People" section: first five rows, "Show all (N)" into a Sheet
 * of the same rows (the Groups section's shape).
 */
export function BundlePermissionPeople({
  users,
  manageableProducers,
  showHeading,
  onEdit,
  onRemove,
}: BundlePermissionPeopleProps) {
  const [showAll, setShowAll] = useState(false)

  const renderRow = (user: BundlePermissionUser) => (
    <BundlePermissionUserRow
      user={user}
      manageableProducers={manageableProducers}
      onEdit={onEdit}
      onRemove={onRemove}
    />
  )

  return (
    <section className="space-y-2">
      {showHeading && <h3 className="text-sm font-medium">People</h3>}
      <PreviewList
        items={users}
        getKey={(user) => user.user_id}
        renderItem={renderRow}
        // Rendered only from a loaded overview that holds a person.
        errorFallback="Couldn't load people."
        empty={null}
        onShowAll={() => setShowAll(true)}
      />

      <Sheet open={showAll} onOpenChange={setShowAll}>
        <SheetContent side="right" className="w-full sm:max-w-lg">
          <SheetHeader>
            <SheetTitle>People with access</SheetTitle>
            <SheetDescription>
              {pluralize(users.length, "person", "people")}
            </SheetDescription>
          </SheetHeader>
          <div className="flex-1 overflow-y-auto px-4 pb-4">
            <ListRowGroup>
              {users.map((user) => (
                <Fragment key={user.user_id}>{renderRow(user)}</Fragment>
              ))}
            </ListRowGroup>
          </div>
        </SheetContent>
      </Sheet>
    </section>
  )
}
