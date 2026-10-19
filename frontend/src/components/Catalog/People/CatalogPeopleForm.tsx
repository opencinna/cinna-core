import { AlertCircle } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"

import type { CatalogGrant, CatalogGrantIn } from "@/client"
import { ListRowGroup } from "@/components/Common/ListRow"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { DialogFooter } from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  type BufferedGrant,
  type CatalogAccess,
  type CatalogPrincipal,
  principalKey,
  principalKind,
  toBufferedGrants,
} from "@/utils/catalogAccess"
import {
  CATALOG_VISIBILITY_OPTIONS,
  type CatalogKind,
  type CatalogVisibility,
} from "@/utils/catalogItems"
import { CatalogGrantRow } from "./CatalogGrantRow"
import {
  CatalogPrincipalPicker,
  type PickedPrincipalDetail,
} from "./CatalogPrincipalPicker"

const NO_MAINTAINER = "At least one person must be a maintainer."

/** Who sees an item at Private, in the kind's own words for `edit`. */
const PRIVATE_AUDIENCE: Record<CatalogKind, string> = {
  desktop: "Only people who can share changes or maintain it see it.",
  skill: "Only people who can publish versions or maintain it see it.",
  cloud: "Only its maintainers see it.",
}

function signature(rows: BufferedGrant[]): string {
  return rows
    .map((r) => `${r.key}=${r.access}`)
    .sort()
    .join("|")
}

export interface CatalogPeopleSave {
  visibility: CatalogVisibility
  grants: CatalogGrantIn[]
}

interface CatalogPeopleFormProps {
  grants: CatalogGrant[]
  kind: CatalogKind
  visibility: CatalogVisibility
  isSaving: boolean
  /** A server refusal (409/422 sentence), shown in the maintainer Alert's slot. */
  refusal: string | null
  onEdited: () => void
  onSave: (next: CatalogPeopleSave) => void
  onCancel: () => void
}

/**
 * The buffered body of the People dialog: General access, the grant rows, the
 * add picker, the maintainer check and the footer. Nothing is written until
 * Save, which sends the whole list (full replace). Rows keep their place when
 * their access changes; a newly added principal goes to the end at Can use.
 */
export function CatalogPeopleForm({
  grants,
  kind,
  visibility: initialVisibility,
  isSaving,
  refusal,
  onEdited,
  onSave,
  onCancel,
}: CatalogPeopleFormProps) {
  const initial = useMemo(() => toBufferedGrants(grants), [grants])
  const [rows, setRows] = useState<BufferedGrant[]>(initial)
  const [visibility, setVisibility] =
    useState<CatalogVisibility>(initialVisibility)

  const dirty =
    visibility !== initialVisibility || signature(rows) !== signature(initial)
  const hasPersonMaintainer = rows.some(
    (r) => r.access === "maintain" && principalKind(r.principal) === "user",
  )
  const hasInertUse =
    visibility !== "users" && rows.some((r) => r.access === "use")
  const grantedKeys = useMemo(() => new Set(rows.map((r) => r.key)), [rows])
  const alertText = refusal || (hasPersonMaintainer ? null : NO_MAINTAINER)

  const edit = (next: BufferedGrant[]) => {
    setRows(next)
    onEdited()
  }

  const changeAccess = (key: string, access: CatalogAccess) =>
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

  const add = (
    principal: CatalogPrincipal,
    title: string,
    { memberCount }: PickedPrincipalDetail,
  ) =>
    edit([
      ...rows,
      {
        key: principalKey(principal),
        principal,
        access: "use",
        title,
        memberCount,
      },
    ])

  return (
    <>
      <div ref={bodyRef} className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="catalog-general-access">General access</Label>
          <Select
            value={visibility}
            onValueChange={(v) => {
              setVisibility(v as CatalogVisibility)
              onEdited()
            }}
            disabled={isSaving}
          >
            <SelectTrigger id="catalog-general-access" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {CATALOG_VISIBILITY_OPTIONS.map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {visibility === "private" && (
            <p className="text-xs text-muted-foreground">
              {PRIVATE_AUDIENCE[kind]}
            </p>
          )}
        </div>

        <div className="space-y-2">
          <h3 className="text-sm font-medium">People, groups and roles</h3>
          {rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No one is listed yet. Add people, groups or roles below.
            </p>
          ) : (
            <div className="max-h-[40vh] overflow-y-auto pr-2">
              {/* `mx-0`: inside a scroll box the group's bleed would put each
                  row's remove button back under the scrollbar. */}
              <ListRowGroup className="mx-0">
                {rows.map((row) => (
                  <CatalogGrantRow
                    key={row.key}
                    grant={row}
                    kind={kind}
                    disabled={isSaving}
                    onAccessChange={changeAccess}
                    onRemove={remove}
                  />
                ))}
              </ListRowGroup>
            </div>
          )}
          {hasInertUse && (
            <p className="text-xs text-muted-foreground">
              Can use rows apply only when access is limited to the people,
              groups and roles below.
            </p>
          )}
          <CatalogPrincipalPicker
            grantedKeys={grantedKeys}
            onPick={add}
            disabled={isSaving}
          />
        </div>
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
            onSave({
              visibility,
              grants: rows.map((r) => ({
                principal: r.principal,
                access: r.access,
              })),
            })
          }
        >
          {isSaving ? "Saving…" : "Save"}
        </LoadingButton>
      </DialogFooter>
    </>
  )
}
