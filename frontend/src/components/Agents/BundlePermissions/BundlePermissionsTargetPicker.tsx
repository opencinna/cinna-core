import {
  type GroupSelectedItem,
  UserAllowlistPicker,
  type UserAllowlistSelectedItem,
} from "@/components/Common/UserAllowlistPicker"
import { Label } from "@/components/ui/label"

interface BundlePermissionsTargetPickerProps {
  enabled: boolean
  /** Whether a group may be picked instead of a person. */
  groupsOffered: boolean
  excludeUserIds: string[]
  excludeGroupIds?: string[]
  /** Offer the signed-in user too (a producer owner may grant themselves). */
  includeSelf?: boolean
  /** The label's classes, so a host form's labels can match each other. */
  labelClassName?: string
  selectedUser: UserAllowlistSelectedItem | null
  selectedGroup: GroupSelectedItem | null
  /** Person picked (``email`` kept apart from the pill label) or cleared. */
  onUserChange: (
    user: UserAllowlistSelectedItem | null,
    email: string | null,
  ) => void
  /** Group picked or cleared. */
  onGroupChange: (group: GroupSelectedItem | null) => void
}

/**
 * The add-mode target: one person, or (when offered) one group. Picking one
 * clears the other.
 */
export function BundlePermissionsTargetPicker({
  enabled,
  groupsOffered,
  excludeUserIds,
  excludeGroupIds,
  includeSelf,
  labelClassName = "text-xs text-muted-foreground",
  selectedUser,
  selectedGroup,
  onUserChange,
  onGroupChange,
}: BundlePermissionsTargetPickerProps) {
  return (
    <UserAllowlistPicker
      enabled={enabled}
      selected={selectedUser ? [selectedUser] : []}
      excludeUserIds={excludeUserIds}
      includeSelf={includeSelf}
      label={
        <Label className={labelClassName}>
          {groupsOffered ? "Person or group" : "User"}
        </Label>
      }
      searchPlaceholder={
        groupsOffered
          ? "Search people or groups to add..."
          : "Search users to add..."
      }
      groups={
        groupsOffered
          ? {
              selected: selectedGroup ? [selectedGroup] : [],
              excludeGroupIds,
              onAdd: (g) => {
                onUserChange(null, null)
                onGroupChange({
                  id: g.id,
                  groupId: g.id,
                  name: g.name,
                  memberCount: g.member_count ?? null,
                })
              },
              onRemove: () => onGroupChange(null),
            }
          : undefined
      }
      onAdd={(u) => {
        onGroupChange(null)
        onUserChange(
          {
            id: u.id,
            userId: u.id,
            fallbackLabel: u.full_name || u.email,
          },
          u.email,
        )
      }}
      onRemove={() => onUserChange(null, null)}
    />
  )
}
