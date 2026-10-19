import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { UserPlus } from "lucide-react"
import { useMemo, useRef, useState } from "react"

import {
  AdminUserGroupsService,
  type UserGroupMemberPublic,
  type UserGroupPublic,
} from "@/client"
import { DataTable } from "@/components/Common/DataTable"
import { DEFAULT_PAGE_SIZE } from "@/components/Common/DataTablePagination"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import { AddUserGroupMembersDialog } from "./AddUserGroupMembersDialog"
import { getMemberColumns } from "./memberColumns"
import { PendingTable } from "./PendingTable"
import { RemoveUserGroupMemberDialog } from "./RemoveUserGroupMemberDialog"
import {
  TableEmptyBlock,
  UserGroupSearchInput,
  useDebouncedValue,
} from "./UserGroupSearchInput"
import { userGroupKeys } from "./userGroupQueries"

/** S5a — who is in the group: searched and paged on the server. */
export function UserGroupMembersTab({ group }: { group: UserGroupPublic }) {
  const [search, setSearch] = useState("")
  const searchRef = useRef<HTMLInputElement>(null)
  const q = useDebouncedValue(search.trim())
  const [pageIndex, setPageIndex] = useState(0)
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [addOpen, setAddOpen] = useState(false)
  const [removeTarget, setRemoveTarget] =
    useState<UserGroupMemberPublic | null>(null)

  const members = useQuery({
    queryKey: [...userGroupKeys.members(group.id), q, pageIndex, pageSize],
    queryFn: () =>
      AdminUserGroupsService.listUserGroupMembers({
        groupId: group.id,
        q: q || undefined,
        skip: pageIndex * pageSize,
        limit: pageSize,
      }),
    placeholderData: keepPreviousData,
  })

  const columns = useMemo(
    () =>
      getMemberColumns({
        groupId: group.id,
        groupName: group.name,
        onRemove: setRemoveTarget,
      }),
    [group.id, group.name],
  )

  const changeSearch = (value: string) => {
    setSearch(value)
    setPageIndex(0)
  }
  const clearSearch = () => {
    changeSearch("")
    searchRef.current?.focus()
  }

  let body: React.ReactNode
  if (members.isError && members.data === undefined) {
    body = (
      <QueryErrorAlert
        error={members.error}
        fallback="Couldn't load the members."
        onRetry={() => members.refetch()}
      />
    )
  } else if (members.data === undefined) {
    body = <PendingTable headers={["Name", "Email", "Added"]} />
  } else {
    body = (
      <DataTable
        columns={columns}
        data={members.data.data}
        getRowId={(member) => member.user_id}
        tableClassName="table-fixed"
        itemLabel="members"
        serverPagination={{
          pageIndex,
          pageSize,
          total: members.data.count,
          onPageChange: setPageIndex,
          onPageSizeChange: setPageSize,
        }}
        emptyState={
          q ? (
            <TableEmptyBlock text={`No members match "${q}".`}>
              <Button variant="link" size="sm" onClick={clearSearch}>
                Clear search
              </Button>
            </TableEmptyBlock>
          ) : (
            <TableEmptyBlock text="No members yet. Add the people this group stands for.">
              <Button size="sm" onClick={() => setAddOpen(true)}>
                <UserPlus className="h-4 w-4" />
                Add members
              </Button>
            </TableEmptyBlock>
          )
        }
      />
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <UserGroupSearchInput
          ref={searchRef}
          value={search}
          onChange={changeSearch}
          onClear={clearSearch}
          ariaLabel="Search members by name or email"
        />
        <Button size="sm" onClick={() => setAddOpen(true)}>
          <UserPlus className="h-4 w-4" />
          Add members
        </Button>
      </div>

      {body}

      <AddUserGroupMembersDialog
        group={group}
        open={addOpen}
        onOpenChange={setAddOpen}
        memberUserIds={(members.data?.data ?? []).map((m) => m.user_id)}
      />
      {removeTarget && (
        <RemoveUserGroupMemberDialog
          group={group}
          member={removeTarget}
          open
          onOpenChange={(open) => {
            if (!open) setRemoveTarget(null)
          }}
          focusAfterRemoval={() => searchRef.current?.focus()}
        />
      )}
    </div>
  )
}
