import { AlertCircle, User, X } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"

import type { ArtifactGrantIn, ArtifactGrantPublic } from "@/client"
import { CatalogPrincipalPicker } from "@/components/Catalog/People/CatalogPrincipalPicker"
import { ListRow, ListRowGroup } from "@/components/Common/ListRow"
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

export type ArtifactVisibility = "private" | "instance"

interface Person {
  userId: string
  name: string
  /** Secondary text that tells two people with one name apart. */
  email: string | null
}

export interface ArtifactPeopleSave {
  visibility: ArtifactVisibility
  grants: ArtifactGrantIn[]
  visibilityChanged: boolean
  grantsChanged: boolean
}

function signature(people: Person[]): string {
  return people
    .map((p) => p.userId)
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
 * people only (D21). Buffered — nothing is written until Save.
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
      grants.map((g) => ({
        userId: g.principal.user_id,
        name: g.display_name || g.email || "Unknown person",
        email: g.email || null,
      })),
    [grants],
  )
  const [people, setPeople] = useState<Person[]>(initial)
  const [visibility, setVisibility] =
    useState<ArtifactVisibility>(initialVisibility)

  const grantsChanged = signature(people) !== signature(initial)
  const visibilityChanged = visibility !== initialVisibility
  const grantedKeys = useMemo(
    () => new Set(people.map((p) => `user:${p.userId}`)),
    [people],
  )

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

  const remove = (userId: string) => {
    const index = people.findIndex((p) => p.userId === userId)
    const neighbour = people[index + 1] ?? people[index - 1]
    setFocusAfterRemove(neighbour ? neighbour.userId : "")
    setPeople(people.filter((p) => p.userId !== userId))
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
          <h3 className="text-sm font-medium">People</h3>
          {people.length === 0 ? (
            <p className="text-sm text-muted-foreground">Nobody else yet.</p>
          ) : (
            <div className="max-h-[40vh] overflow-y-auto pr-2">
              {/* `mx-0`: inside a scroll box the group's bleed would put each
                  row's remove button back under the scrollbar. */}
              <ListRowGroup className="mx-0">
                {people.map((person) => (
                  <ListRow
                    key={person.userId}
                    icon={
                      <span className="flex h-6 w-6 items-center justify-center rounded-md bg-muted">
                        <User className="h-3.5 w-3.5 text-muted-foreground" />
                      </span>
                    }
                    title={person.name}
                    meta={
                      person.email && person.email !== person.name
                        ? person.email
                        : undefined
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
                          data-remove-key={person.userId}
                          disabled={isSaving}
                          onClick={() => remove(person.userId)}
                        >
                          <X className="h-3.5 w-3.5" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="text-xs">
                        Remove {person.name}
                      </TooltipContent>
                    </Tooltip>
                  </ListRow>
                ))}
              </ListRowGroup>
            </div>
          )}
          <CatalogPrincipalPicker
            allowRoles={false}
            grantedKeys={grantedKeys}
            disabled={isSaving}
            onPick={(principal, title, email) => {
              if (!("user_id" in principal)) return
              setPeople([
                ...people,
                {
                  userId: principal.user_id,
                  name: title,
                  email: email || null,
                },
              ])
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
                principal: { user_id: p.userId },
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
