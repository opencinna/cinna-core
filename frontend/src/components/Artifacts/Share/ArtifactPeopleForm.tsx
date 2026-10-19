import { AlertCircle, X } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"

import type { ArtifactGrantIn, ArtifactGrantPublic } from "@/client"
import { CatalogPrincipalPicker } from "@/components/Catalog/People/CatalogPrincipalPicker"
import { ListRow, ListRowGroup, RowInfo } from "@/components/Common/ListRow"
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
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { APP_NAME } from "@/utils"
import { pluralize } from "@/utils/pluralize"
import { PRINCIPAL_KIND_ICON } from "@/utils/principalKinds"

export type ArtifactVisibility = "private" | "instance"

/** A person or a user group on the artifact's list. */
interface Person {
  /** `user:<id>` / `group:<id>` — the picker's granted-key shape. */
  key: string
  kind: "user" | "group"
  id: string
  name: string
  /** Secondary text that tells two people with one name apart. */
  email: string | null
  memberCount: number | null
}

function toPrincipal(person: Person): ArtifactGrantIn["principal"] {
  return person.kind === "group"
    ? { group_id: person.id }
    : { user_id: person.id }
}

export interface ArtifactPeopleSave {
  visibility: ArtifactVisibility
  grants: ArtifactGrantIn[]
  visibilityChanged: boolean
  grantsChanged: boolean
}

function signature(people: Person[]): string {
  return people
    .map((p) => p.key)
    .sort()
    .join("|")
}

interface ArtifactPeopleFormProps {
  grants: ArtifactGrantPublic[]
  visibility: ArtifactVisibility
  isSaving: boolean
  refusal: string | null
  onEdited: () => void
  onSave: (next: ArtifactPeopleSave) => void
  onCancel: () => void
}

/**
 * The People half of the share dialog: the `CatalogPeopleForm` skeleton,
 * people and user groups, no roles (D21) and no level. Buffered — nothing is
 * written until Save.
 */
export function ArtifactPeopleForm({
  grants,
  visibility: initialVisibility,
  isSaving,
  refusal,
  onEdited,
  onSave,
  onCancel,
}: ArtifactPeopleFormProps) {
  const initial = useMemo<Person[]>(
    () =>
      grants.map((g): Person => {
        if ("group_id" in g.principal) {
          const id = g.principal.group_id
          return {
            key: `group:${id}`,
            kind: "group",
            id,
            name: g.display_name || "Unknown group",
            email: null,
            memberCount: g.member_count ?? null,
          }
        }
        const id = g.principal.user_id
        return {
          key: `user:${id}`,
          kind: "user",
          id,
          name: g.display_name || g.email || "Unknown person",
          email: g.email || null,
          memberCount: null,
        }
      }),
    [grants],
  )
  const [people, setPeople] = useState<Person[]>(initial)
  const [visibility, setVisibility] =
    useState<ArtifactVisibility>(initialVisibility)

  const grantsChanged = signature(people) !== signature(initial)
  const visibilityChanged = visibility !== initialVisibility
  const grantedKeys = useMemo(() => new Set(people.map((p) => p.key)), [people])

  // The remove button unmounts with its row; focus moves on deliberately —
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
    const index = people.findIndex((p) => p.key === key)
    const neighbour = people[index + 1] ?? people[index - 1]
    setFocusAfterRemove(neighbour ? neighbour.key : "")
    setPeople(people.filter((p) => p.key !== key))
    onEdited()
  }

  return (
    <>
      <div ref={bodyRef} className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="artifact-general-access">General access</Label>
          <Select
            value={visibility}
            onValueChange={(v) => {
              setVisibility(v as ArtifactVisibility)
              onEdited()
            }}
            disabled={isSaving}
          >
            <SelectTrigger id="artifact-general-access" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="private">Only me</SelectItem>
              <SelectItem value="instance">Everyone on {APP_NAME}</SelectItem>
            </SelectContent>
          </Select>
          {visibility === "private" && (
            <p className="text-xs text-muted-foreground">
              Plus the people below.
            </p>
          )}
        </div>

        <div className="space-y-2">
          <h3 className="text-sm font-medium">People and groups</h3>
          {people.length === 0 ? (
            <p className="text-sm text-muted-foreground">Nobody else yet.</p>
          ) : (
            <div className="max-h-[40vh] overflow-y-auto pr-2">
              {/* `mx-0`: inside a scroll box the group's bleed would put each
                  row's remove button back under the scrollbar. */}
              <ListRowGroup className="mx-0">
                {people.map((person) => {
                  const Icon = PRINCIPAL_KIND_ICON[person.kind]
                  return (
                    <ListRow
                      key={person.key}
                      icon={
                        <span className="flex h-6 w-6 items-center justify-center rounded-md bg-muted">
                          <Icon className="h-3.5 w-3.5 text-muted-foreground" />
                        </span>
                      }
                      title={person.name}
                      meta={
                        person.email && person.email !== person.name
                          ? person.email
                          : undefined
                      }
                      flags={
                        person.kind === "group" &&
                        person.memberCount != null ? (
                          <RowInfo
                            facts={[
                              `Group · ${pluralize(person.memberCount, "member", "members")}`,
                            ]}
                          />
                        ) : undefined
                      }
                    >
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
                            aria-label={`Remove ${person.name}`}
                            data-remove-key={person.key}
                            disabled={isSaving}
                            onClick={() => remove(person.key)}
                          >
                            <X className="h-3.5 w-3.5" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="text-xs">
                          Remove {person.name}
                        </TooltipContent>
                      </Tooltip>
                    </ListRow>
                  )
                })}
              </ListRowGroup>
            </div>
          )}
          <CatalogPrincipalPicker
            allowRoles={false}
            grantedKeys={grantedKeys}
            disabled={isSaving}
            onPick={(principal, title, { email, memberCount }) => {
              let next: Person
              if ("group_id" in principal) {
                next = {
                  key: `group:${principal.group_id}`,
                  kind: "group",
                  id: principal.group_id,
                  name: title,
                  email: null,
                  memberCount: memberCount ?? null,
                }
              } else if ("user_id" in principal) {
                next = {
                  key: `user:${principal.user_id}`,
                  kind: "user",
                  id: principal.user_id,
                  name: title,
                  email: email || null,
                  memberCount: null,
                }
              } else {
                return
              }
              setPeople([...people, next])
              onEdited()
            }}
          />
        </div>
      </div>

      {refusal && (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertDescription className="break-words">{refusal}</AlertDescription>
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
          disabled={!grantsChanged && !visibilityChanged}
          onClick={() =>
            onSave({
              visibility,
              grants: people.map((p) => ({
                principal: toPrincipal(p),
                access: "view",
              })),
              visibilityChanged,
              grantsChanged,
            })
          }
        >
          {isSaving ? "Saving…" : "Save"}
        </LoadingButton>
      </DialogFooter>
    </>
  )
}
