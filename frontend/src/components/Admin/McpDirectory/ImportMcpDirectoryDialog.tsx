import { useMutation, useQueryClient } from "@tanstack/react-query"
import { AlertCircle } from "lucide-react"

import { type McpDirectoryImportResult, McpDirectoryService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import { getErrorMessage } from "@/utils"
import { plural } from "@/utils/marketplace"
import { apiErrorCode } from "./mcpDirectoryApi"
import { MCP_DIRECTORY_KEY, skippedReasonSentence } from "./mcpDirectoryCopy"

interface ImportMcpDirectoryDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

function importErrorSentence(error: unknown): string {
  if (apiErrorCode(error) === "index_version_unsupported") {
    return "The opencinna index uses a format this server does not read yet."
  }
  return getErrorMessage(error, "Couldn't read the opencinna index.")
}

/**
 * Pull the opencinna index into the directory (S8). One sentence and one
 * button; the body then swaps to the counts. No success toast: the panel is
 * the feedback while it is open, and it names the follow-up (stale rows).
 *
 * The caller mounts it fresh per open (`key`), so a second import starts at
 * the question rather than at the last result.
 */
export function ImportMcpDirectoryDialog({
  open,
  onOpenChange,
}: ImportMcpDirectoryDialogProps) {
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationFn: () => McpDirectoryService.importMcpDirectoryIndex(),
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: MCP_DIRECTORY_KEY }),
  })
  const pending = mutation.isPending

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!pending) onOpenChange(next)
      }}
    >
      <DialogContent
        className="max-h-[85vh] overflow-x-hidden overflow-y-auto sm:max-w-md [&>*]:min-w-0"
        onEscapeKeyDown={(e) => {
          if (pending) e.preventDefault()
        }}
        onInteractOutside={(e) => {
          if (pending) e.preventDefault()
        }}
      >
        <DialogHeader>
          <DialogTitle>Import from opencinna</DialogTitle>
          {!mutation.data && (
            <DialogDescription>
              Adds and refreshes the MCP servers listed at opencinna.io. Servers
              you added by hand are never overwritten.
            </DialogDescription>
          )}
        </DialogHeader>

        {mutation.data ? (
          <ImportResultPanel result={mutation.data} />
        ) : (
          mutation.isError && (
            <Alert variant="destructive">
              <AlertCircle />
              <AlertDescription className="break-words">
                {importErrorSentence(mutation.error)}
              </AlertDescription>
            </Alert>
          )
        )}

        <DialogFooter>
          {mutation.data ? (
            <Button type="button" onClick={() => onOpenChange(false)}>
              Done
            </Button>
          ) : (
            <>
              <Button
                type="button"
                variant="outline"
                disabled={pending}
                onClick={() => onOpenChange(false)}
              >
                Cancel
              </Button>
              <LoadingButton
                type="button"
                loading={pending}
                onClick={() => mutation.mutate()}
              >
                {pending ? "Importing…" : "Import"}
              </LoadingButton>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function Fact({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="grid grid-cols-[9rem_minmax(0,1fr)] gap-3 text-sm">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  )
}

const servers = (n: number) => plural(n, "server")

/**
 * The counts, with noun labels and units. A zero renders nothing — except
 * Added, which answers the question the admin clicked to ask.
 */
function ImportResultPanel({ result }: { result: McpDirectoryImportResult }) {
  const updated = result.updated ?? 0
  const unchanged = result.unchanged ?? 0
  const stale = result.stale ?? []
  const skipped = result.skipped ?? []

  return (
    <dl className="grid gap-2">
      <Fact label="Added">{servers(result.created ?? 0)}</Fact>
      {updated > 0 && <Fact label="Updated">{servers(updated)}</Fact>}
      {unchanged > 0 && <Fact label="Unchanged">{servers(unchanged)}</Fact>}
      {stale.length > 0 && (
        <Fact label="No longer listed">
          <p>{servers(stale.length)}</p>
          <p className="text-xs text-muted-foreground">
            Kept here — delete them if you no longer want them.
          </p>
          <ul className="mt-1.5 max-h-[40vh] space-y-1 overflow-y-auto pr-2">
            {stale.map((name, index) => (
              <li
                key={`${name}-${index}`}
                className="truncate font-mono text-xs"
              >
                {name}
              </li>
            ))}
          </ul>
        </Fact>
      )}
      {skipped.length > 0 && (
        <Fact label="Skipped">
          <p>{servers(skipped.length)}</p>
          <ul className="mt-1.5 max-h-[40vh] space-y-1.5 overflow-y-auto pr-2">
            {skipped.map((item, index) => (
              // Keyed by position too: the index can list one id twice.
              <li key={`${item.id}-${index}`} className="min-w-0">
                <p className="truncate font-mono text-xs">{item.id}</p>
                <p className="text-xs text-muted-foreground">
                  {skippedReasonSentence(item.reason)}
                </p>
              </li>
            ))}
          </ul>
        </Fact>
      )}
    </dl>
  )
}
