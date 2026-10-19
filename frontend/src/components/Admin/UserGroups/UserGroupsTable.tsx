import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useMemo, useRef, useState } from "react"

import { AdminUserGroupsService, type UserGroupPublic } from "@/client"
import { DataTable } from "@/components/Common/DataTable"
import { DEFAULT_PAGE_SIZE } from "@/components/Common/DataTablePagination"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import {
  DeleteUserGroupDialog,
  deleteUserGroupMutationKey,
} from "./DeleteUserGroupDialog"
import { PendingTable } from "./PendingTable"
import { UserGroupFormDialog } from "./UserGroupFormDialog"
import {
  TableEmptyBlock,
  UserGroupSearchInput,
  useDebouncedValue,
} from "./UserGroupSearchInput"
import { getUserGroupColumns } from "./userGroupColumns"
import { userGroupKeys } from "./userGroupQueries"

/**
 * S2 body — every group, searched and paged on the server. The page header
 * owns "Create group"; the empty state repeats it, so `onCreate` comes in.
 */
export function UserGroupsTable({ onCreate }: { onCreate: () => void }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [search, setSearch] = useState("")
  const searchRef = useRef<HTMLInputElement>(null)
  const q = useDebouncedValue(search.trim())
  const [pageIndex, setPageIndex] = useState(0)
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [editTarget, setEditTarget] = useState<UserGroupPublic | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<UserGroupPublic | null>(
    null,
  )

  const groups = useQuery({
    queryKey: [...userGroupKeys.list, q, pageIndex, pageSize],
    queryFn: () =>
      AdminUserGroupsService.listUserGroups({
        q: q || undefined,
        skip: pageIndex * pageSize,
        limit: pageSize,
      }),
    placeholderData: keepPreviousData,
  })

  const columns = useMemo(
    () =>
      getUserGroupColumns({ onEdit: setEditTarget, onDelete: setDeleteTarget }),
    [],
  )

  const changeSearch = (value: string) => {
    setSearch(value)
    setPageIndex(0)
  }
  // Both clear controls unmount on click (the X and the no-match link), so
  // focus moves to the input rather than falling to the page.
  const clearSearch = () => {
    changeSearch("")
    searchRef.current?.focus()
  }

  let body: React.ReactNode
  if (groups.isError && groups.data === undefined) {
    body = (
      <QueryErrorAlert
        error={groups.error}
        fallback="Couldn't load the groups."
        onRetry={() => groups.refetch()}
      />
    )
  } else if (groups.data === undefined) {
    body = <PendingTable headers={["Name", "Members", "Shared", "Created"]} />
  } else {
    body = (
      <DataTable
        columns={columns}
        data={groups.data.data}
        getRowId={(group) => group.id}
        tableClassName="table-fixed"
        itemLabel="groups"
        serverPagination={{
          pageIndex,
          pageSize,
          total: groups.data.count,
          onPageChange: setPageIndex,
          onPageSizeChange: setPageSize,
        }}
        emptyState={
          q ? (
            <TableEmptyBlock text={`No groups match "${q}".`}>
              <Button variant="link" size="sm" onClick={clearSearch}>
                Clear search
              </Button>
            </TableEmptyBlock>
          ) : (
            <TableEmptyBlock text="No groups yet. Create one, then share with it from any Share dialog.">
              <Button size="sm" onClick={onCreate}>
                Create group
              </Button>
            </TableEmptyBlock>
          )
        }
        onRowClick={(group) => {
          // A row whose delete is in flight is not a way into it.
          if (
            queryClient.isMutating({
              mutationKey: deleteUserGroupMutationKey(group.id),
            }) > 0
          ) {
            return
          }
          void navigate({
            to: "/admin/user-group/$groupId",
            params: { groupId: group.id },
          })
        }}
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
          ariaLabel="Search groups by name"
        />
      </div>

      {body}

      {editTarget && (
        <UserGroupFormDialog
          open
          onOpenChange={(open) => {
            if (!open) setEditTarget(null)
          }}
          group={editTarget}
        />
      )}
      {deleteTarget && (
        <DeleteUserGroupDialog
          group={deleteTarget}
          open
          onOpenChange={(open) => {
            if (!open) setDeleteTarget(null)
          }}
        />
      )}
    </div>
  )
}
