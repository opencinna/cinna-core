import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Pencil } from "lucide-react"
import { useState } from "react"

import {
  type ApiError,
  type UserDetailsPublic,
  UsersService,
} from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"

const DETAILS_PLACEHOLDER = `real name = Master of the universe
favorite food = hotdogs
# lines starting with # are ignored`

/**
 * Render the server-normalized details map as `KEY="value"` lines. The server
 * is the source of truth for normalization, so this only displays what the
 * backend returned in `details_parsed`.
 */
function formatParsedDetails(
  parsed: UserDetailsPublic["details_parsed"],
): string {
  if (!parsed) {
    return ""
  }
  return Object.entries(parsed)
    .map(([key, value]) => {
      const escaped = String(value).replace(/"/g, '\\"')
      return `${key}="${escaped}"`
    })
    .join("\n")
}

/**
 * Extract the 422 `detail` string from an API error body, if present.
 * Returns null for non-422 errors or unexpected body shapes.
 */
function extractValidationDetail(err: ApiError): string | null {
  if (err.status !== 422) {
    return null
  }
  const detail = (err.body as { detail?: unknown } | undefined)?.detail
  if (typeof detail === "string") {
    return detail
  }
  return null
}

export function UserDetailsSettings() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState("")
  const [inlineError, setInlineError] = useState<string | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ["user-details"],
    queryFn: () => UsersService.readUserDetailsMe(),
  })

  const updateMutation = useMutation({
    mutationFn: (detailsRaw: string) =>
      UsersService.updateUserDetailsMe({
        requestBody: { details_raw: detailsRaw },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["user-details"] })
      showSuccessToast("Details updated")
      setOpen(false)
      setInlineError(null)
    },
    onError: (err: ApiError) => {
      const detail = extractValidationDetail(err)
      if (detail) {
        // Keep the editor open so the user can fix the input.
        setInlineError(detail)
      } else {
        showErrorToast("Failed to update details")
      }
    },
  })

  const normalizedView = formatParsedDetails(data?.details_parsed ?? null)
  const hasDetails = normalizedView.length > 0

  const startEditing = () => {
    setDraft(data?.details_raw ?? "")
    setInlineError(null)
    setOpen(true)
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle>User's Details</CardTitle>
          {!isLoading && (
            <Button size="sm" variant="outline" onClick={startEditing}>
              <Pencil className="h-4 w-4 mr-2" />
              Edit
            </Button>
          )}
        </div>
        <CardDescription>
          Free-text <code>KEY = value</code> notes about you, made available to
          your agents as <code>current_user.custom_details</code>.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {isLoading ? (
          <div className="space-y-3">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-2/3" />
          </div>
        ) : (
          <div className="space-y-3">
            {hasDetails ? (
              <pre className="rounded-md border bg-muted/50 p-3 text-sm font-mono whitespace-pre-wrap break-words">
                {normalizedView}
              </pre>
            ) : (
              <p className="text-sm text-muted-foreground">
                No details set yet.
              </p>
            )}
          </div>
        )}
      </CardContent>

      <Dialog
        open={open}
        onOpenChange={(v) => {
          setOpen(v)
          if (!v) setInlineError(null)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit User's Details</DialogTitle>
            <DialogDescription>
              One <code>KEY = value</code> per line. Keys are normalized to{" "}
              <code>UPPER_SNAKE_CASE</code>. Lines starting with <code>#</code>{" "}
              are ignored.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <Textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={DETAILS_PLACEHOLDER}
              rows={8}
              className="font-mono text-sm"
              aria-invalid={inlineError ? true : undefined}
            />
            {inlineError && (
              <p className="text-sm text-destructive whitespace-pre-wrap">
                {inlineError}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => setOpen(false)}
                disabled={updateMutation.isPending}
              >
                Cancel
              </Button>
              <LoadingButton
                size="sm"
                loading={updateMutation.isPending}
                onClick={() => updateMutation.mutate(draft)}
              >
                Save
              </LoadingButton>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
