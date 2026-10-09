import { AlertCircle } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"

import type { LibraryGrantIn, LibraryGrantPublic } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { DialogFooter } from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  compareGrants,
  type LibraryAccess,
  type LibraryPrincipal,
  principalKey,
  principalKind,
  principalLabel,
} from "@/utils/libraryAccess"
import { type BufferedGrant, LibraryGrantRow } from "./LibraryGrantRow"
import { LibraryPrincipalPicker } from "./LibraryPrincipalPicker"

const NO_MAINTAINER =
  "An agent in the team library needs at least one maintainer."

function toBuffer(grants: LibraryGrantPublic[]): BufferedGrant[] {
  return grants
    .map((g) => ({
      key: principalKey(g.principal),
      principal: g.principal,
      access: g.access,
      title: principalLabel(g.principal, g.display_name),
    }))
    .sort(compareGrants)
}

function signature(rows: BufferedGrant[]): string {
  return rows
    .map((r) => `${r.key}=${r.access}`)
    .sort()
    .join("|")
}

interface LibraryAccessFormProps {
  grants: LibraryGrantPublic[]
  isSaving: boolean
  /** A server refusal (409/422 sentence), shown in the maintainer Alert's slot. */
  refusal: string | null
  onEdited: () => void
  onSave: (grants: LibraryGrantIn[]) => void
  onCancel: () => void
}

/**
 * The buffered body of the access dialog: rows, the add picker, the
 * maintainer check and the footer. Nothing is written until Save, which sends
 * the whole list (full replace). Rows keep their place when their access
 * changes; a newly added principal goes to the end at Can get.
 */
export function LibraryAccessForm({
  grants,
  isSaving,
  refusal,
  onEdited,
  onSave,
  onCancel,
}: LibraryAccessFormProps) {
  const initial = useMemo(() => toBuffer(grants), [grants])
  const [rows, setRows] = useState<BufferedGrant[]>(initial)

  const dirty = signature(rows) !== signature(initial)
  const hasPersonMaintainer = rows.some(
    (r) => r.access === "maintain" && principalKind(r.principal) === "user",
  )
  const grantedKeys = useMemo(() => new Set(rows.map((r) => r.key)), [rows])
  const alertText = refusal || (hasPersonMaintainer ? null : NO_MAINTAINER)

  const edit = (next: BufferedGrant[]) => {
    setRows(next)
    onEdited()
  }

  const changeAccess = (key: string, access: LibraryAccess) =>
    edit(rows.map((r) => (r.key === key ? { ...r, access } : r)))

  // The remove button unmounts with its row; move focus on deliberately —
  // the next row's remove, else the previous one's, else the add trigger.
  const bodyRef = useRef<HTMLDivElement>(null)
  const [focusAfterRemove, setFocusAfterRemove] = useState<string | null>(null)
  useEffect(() => {
    if (focusAfterRemove === null) return
    const target = bodyRef.current?.querySelector<HTMLElement>(
      focusAfterRemove === ""
        ? "[data-add-principal]"
        : `[data-remove-key="${CSS.escape(focusAfterRemove)}"]`,
    )
    target?.focus()
    setFocusAfterRemove(null)
  }, [focusAfterRemove])

  const remove = (key: string) => {
    const index = rows.findIndex((r) => r.key === key)
    const neighbour = rows[index + 1] ?? rows[index - 1]
    setFocusAfterRemove(neighbour ? neighbour.key : "")
    edit(rows.filter((r) => r.key !== key))
  }

  const add = (principal: LibraryPrincipal, title: string) =>
    edit([
      ...rows,
      { key: principalKey(principal), principal, access: "read", title },
    ])

  return (
    <>
      <div ref={bodyRef} className="space-y-3">
        <h3 className="text-sm font-medium">Access</h3>
        {rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No one has access yet. Add people or roles below.
          </p>
        ) : (
          <div className="max-h-[40vh] overflow-y-auto pr-2">
            {/* `mx-0`: inside a scroll box the group's bleed would put each
                row's remove button back under the scrollbar. */}
            <ListRowGroup className="mx-0">
              {rows.map((row) => (
                <LibraryGrantRow
                  key={row.key}
                  grant={row}
                  disabled={isSaving}
                  onAccessChange={changeAccess}
                  onRemove={remove}
                />
              ))}
            </ListRowGroup>
          </div>
        )}
        <LibraryPrincipalPicker
          grantedKeys={grantedKeys}
          onPick={add}
          disabled={isSaving}
        />
      </div>

      {alertText && (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertDescription className="break-words">
            {alertText}
          </AlertDescription>
        </Alert>
      )}

      <DialogFooter>
        <Button
          type="button"
          variant="outline"
          onClick={onCancel}
          disabled={isSaving}
        >
          Cancel
        </Button>
        <LoadingButton
          type="button"
          loading={isSaving}
          disabled={!dirty || !hasPersonMaintainer}
          onClick={() =>
            onSave(
              rows.map((r) => ({ principal: r.principal, access: r.access })),
            )
          }
        >
          {isSaving ? "Saving…" : "Save"}
        </LoadingButton>
      </DialogFooter>
    </>
  )
}
