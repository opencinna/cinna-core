import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Info } from "lucide-react"
import { useMemo, useState } from "react"

import { type McpDirectoryEntryPublic, McpDirectoryService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { McpEntryConnectionFields } from "./McpEntryConnectionFields"
import { McpEntryListingFields } from "./McpEntryListingFields"
import { McpEnvVarsEditor } from "./McpEnvVarsEditor"
import {
  MCP_DIRECTORY_KEY,
  TRANSPORT_COPY,
  TRANSPORT_ORDER,
} from "./mcpDirectoryCopy"
import {
  draftFromEntry,
  draftSignature,
  hasErrors,
  type McpDraftErrors,
  type McpEntryDraft,
  type McpTransport,
  toUpdateBody,
  validateConnection,
  validateListing,
} from "./mcpEntryDraft"

interface EditMcpServerSheetProps {
  entry: McpDirectoryEntryPublic
  onClose: () => void
}

/**
 * The whole configuration of one server, structured (S6): Listing,
 * Connection, Environment, with Save / Cancel and dirty tracking. The fields
 * are the Add wizard's, so create and edit cannot drift. The caller mounts it
 * per entry, so the draft is seeded once from the row that was opened.
 */
export function EditMcpServerSheet({
  entry,
  onClose,
}: EditMcpServerSheetProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const initial = useMemo(() => draftFromEntry(entry), [entry])
  const [draft, setDraft] = useState<McpEntryDraft>(initial)
  const [errors, setErrors] = useState<McpDraftErrors>({})
  const [confirmDiscard, setConfirmDiscard] = useState(false)

  const baseline = useMemo(() => draftSignature(initial), [initial])
  const dirty = draftSignature(draft) !== baseline

  const patch = (next: Partial<McpEntryDraft>) =>
    setDraft((prev) => ({ ...prev, ...next }))

  const mutation = useMutation({
    mutationFn: () =>
      McpDirectoryService.updateMcpDirectoryEntry({
        entryId: entry.id,
        requestBody: toUpdateBody(draft, initial),
      }),
    onSuccess: (saved) => {
      showSuccessToast(`${saved.title} saved`)
      void queryClient.invalidateQueries({ queryKey: MCP_DIRECTORY_KEY })
      onClose()
    },
    onError: (error) =>
      showErrorToast(getErrorMessage(error, `Couldn't save ${entry.title}`)),
  })
  const pending = mutation.isPending

  const requestClose = () => {
    if (pending) return
    if (dirty) setConfirmDiscard(true)
    else onClose()
  }

  const save = () => {
    const found = {
      ...validateListing(draft, { checkName: false }),
      ...validateConnection(draft),
    }
    setErrors(found)
    if (!hasErrors(found)) mutation.mutate()
  }

  return (
    <>
      <Sheet
        open
        onOpenChange={(open) => {
          if (!open) requestClose()
        }}
      >
        <SheetContent
          side="right"
          className="w-full sm:max-w-lg"
          // A save in flight owns the Sheet.
          onEscapeKeyDown={(e) => {
            if (pending) e.preventDefault()
          }}
          onInteractOutside={(e) => {
            if (pending) e.preventDefault()
          }}
        >
          <SheetHeader>
            <SheetTitle className="break-words">{entry.title}</SheetTitle>
            <SheetDescription className="truncate font-mono">
              {entry.name}
            </SheetDescription>
          </SheetHeader>

          <form
            id="edit-mcp-server"
            className="flex-1 overflow-y-auto px-4"
            onSubmit={(e) => {
              e.preventDefault()
              save()
            }}
          >
            <fieldset disabled={pending} className="min-w-0 space-y-5 pb-4">
              {entry.source === "opencinna" && (
                <Alert>
                  <Info />
                  <AlertDescription>
                    Imported from the opencinna index — the next import
                    overwrites changes made here.
                  </AlertDescription>
                </Alert>
              )}

              <section className="space-y-3">
                <h3 className="text-sm font-medium">Listing</h3>
                <McpEntryListingFields
                  draft={draft}
                  onChange={patch}
                  errors={errors}
                  idPrefix="edit-mcp"
                  nameMode="readonly"
                  optionalFields="inline"
                />
              </section>

              <Separator />

              <section className="space-y-3">
                <h3 className="text-sm font-medium">Connection</h3>
                <div className="grid gap-1.5">
                  <Label htmlFor="edit-mcp-transport">Transport</Label>
                  <Select
                    value={draft.transport ?? undefined}
                    onValueChange={(value) =>
                      patch({ transport: value as McpTransport })
                    }
                  >
                    <SelectTrigger id="edit-mcp-transport" className="w-full">
                      <SelectValue placeholder="Choose a transport" />
                    </SelectTrigger>
                    <SelectContent>
                      {TRANSPORT_ORDER.map((value) => (
                        <SelectItem key={value} value={value}>
                          {TRANSPORT_COPY[value].label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {errors.transport && (
                    <p className="text-xs text-destructive">
                      {errors.transport}
                    </p>
                  )}
                </div>
                <McpEntryConnectionFields
                  draft={draft}
                  onChange={patch}
                  errors={errors}
                  idPrefix="edit-mcp"
                />
              </section>

              <Separator />

              <section className="space-y-3">
                <h3 className="sr-only">Environment</h3>
                <McpEnvVarsEditor
                  value={draft.env}
                  onChange={(env) => patch({ env })}
                  errors={errors.env}
                  countError={errors.envCount}
                  idPrefix="edit-mcp"
                />
              </section>
            </fieldset>
          </form>

          <SheetFooter className="flex-row justify-end border-t">
            <Button
              type="button"
              variant="outline"
              disabled={pending}
              onClick={requestClose}
            >
              Cancel
            </Button>
            <LoadingButton
              type="submit"
              form="edit-mcp-server"
              loading={pending}
              disabled={!dirty}
            >
              {pending ? "Saving…" : "Save"}
            </LoadingButton>
          </SheetFooter>
        </SheetContent>
      </Sheet>

      <AlertDialog open={confirmDiscard} onOpenChange={setConfirmDiscard}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard changes?</AlertDialogTitle>
            <AlertDialogDescription>
              Your edits to <em>{entry.title}</em> have not been saved.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setConfirmDiscard(false)
                onClose()
              }}
            >
              Discard changes
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
