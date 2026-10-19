import { useQuery } from "@tanstack/react-query"
import {
  BookOpen,
  FileText,
  KeyRound,
  Layers,
  Library,
  ShieldCheck,
  Sparkles,
} from "lucide-react"
import { useMemo, useRef, useState } from "react"

import {
  AdminUserGroupsService,
  type UserGroupPublic,
  type UserGroupSharePublic,
} from "@/client"
import {
  CatalogFilterMenu,
  type CatalogFilterOption,
} from "@/components/Catalog/CatalogFilterMenu"
import { DataTable } from "@/components/Common/DataTable"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Button } from "@/components/ui/button"
import {
  shareFamily,
  shareResourceName,
  shareTypeLabel,
  type UserGroupShareFamily,
} from "@/utils/userGroupShareTypes"
import { PendingTable } from "./PendingTable"
import { RevokeUserGroupShareDialog } from "./RevokeUserGroupShareDialog"
import { getShareColumns } from "./shareColumns"
import { TableEmptyBlock, UserGroupSearchInput } from "./UserGroupSearchInput"
import { userGroupKeys } from "./userGroupQueries"

const FAMILY_FILTERS: readonly CatalogFilterOption<UserGroupShareFamily>[] = [
  { value: "all", label: "All", icon: Layers },
  { value: "catalog", label: "Catalog", icon: Library },
  { value: "credential", label: "Credentials", icon: KeyRound },
  { value: "ai_credential", label: "AI credentials", icon: Sparkles },
  { value: "artifact", label: "Artifacts", icon: FileText },
  { value: "knowledge_source", label: "Knowledge sources", icon: BookOpen },
  { value: "agent_api", label: "API scopes", icon: ShieldCheck },
]

function matches(share: UserGroupSharePublic, query: string): boolean {
  return [
    shareResourceName(share),
    shareTypeLabel(share.resource_type),
    share.owner?.display_name,
  ]
    .filter(Boolean)
    .some((field) => (field as string).toLowerCase().includes(query))
}

/**
 * S5b — everything shared with the group, filtered by family and searched in
 * the browser (the endpoint returns the whole list). No Add control: grants
 * are made in each resource's own Share dialog (D3).
 */
export function UserGroupSharesTab({ group }: { group: UserGroupPublic }) {
  const [family, setFamily] = useState<UserGroupShareFamily>("all")
  const [search, setSearch] = useState("")
  const searchRef = useRef<HTMLInputElement>(null)
  const [revokeTarget, setRevokeTarget] = useState<UserGroupSharePublic | null>(
    null,
  )

  const shares = useQuery({
    queryKey: userGroupKeys.shares(group.id),
    queryFn: () =>
      AdminUserGroupsService.listUserGroupShares({ groupId: group.id }),
  })

  const columns = useMemo(
    () =>
      getShareColumns({
        groupId: group.id,
        groupName: group.name,
        onRevoke: setRevokeTarget,
      }),
    [group.id, group.name],
  )

  const query = search.trim().toLowerCase()
  const visible = useMemo(
    () =>
      (shares.data?.data ?? []).filter(
        (share) =>
          (family === "all" || shareFamily(share.resource_type) === family) &&
          (!query || matches(share, query)),
      ),
    [shares.data, family, query],
  )

  const clearSearch = () => {
    setSearch("")
    searchRef.current?.focus()
  }

  const narrowed = family !== "all" || query !== ""
  const emptyState = narrowed ? (
    <TableEmptyBlock text="Nothing shared with this group matches.">
      <Button
        variant="link"
        size="sm"
        onClick={() => {
          setFamily("all")
          clearSearch()
        }}
      >
        Show everything
      </Button>
    </TableEmptyBlock>
  ) : (
    // Under D3 this empty state has no action: nothing is shared from here.
    <TableEmptyBlock text="Nothing is shared with this group yet. Owners share from each item's own Share dialog." />
  )

  let body: React.ReactNode
  if (shares.isError && shares.data === undefined) {
    body = (
      <QueryErrorAlert
        error={shares.error}
        fallback="Couldn't load what is shared with this group."
        onRetry={() => shares.refetch()}
      />
    )
  } else if (shares.data === undefined) {
    body = (
      <PendingTable
        headers={["Type", "Name", "Level", "Granted by", "Granted"]}
      />
    )
  } else {
    body = (
      <DataTable
        // A new filter or search starts at page 1 (remount); a refetch after
        // a revoke keeps the page the admin is on.
        key={`${family}|${query}`}
        autoResetPageIndex={false}
        columns={columns}
        data={visible}
        getRowId={(share) => `${share.resource_type}:${share.resource_id}`}
        tableClassName="table-fixed"
        itemLabel="items"
        emptyState={emptyState}
      />
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <CatalogFilterMenu
          options={FAMILY_FILTERS}
          value={family}
          onChange={setFamily}
          allValue="all"
        />
        <UserGroupSearchInput
          ref={searchRef}
          value={search}
          onChange={setSearch}
          onClear={clearSearch}
          ariaLabel="Search shared items by name, type or owner"
        />
      </div>

      {body}

      {revokeTarget && (
        <RevokeUserGroupShareDialog
          group={group}
          share={revokeTarget}
          open
          onOpenChange={(open) => {
            if (!open) setRevokeTarget(null)
          }}
          focusAfterRevoke={() => searchRef.current?.focus()}
        />
      )}
    </div>
  )
}
