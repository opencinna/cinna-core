import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { FileText } from "lucide-react"
import { useState } from "react"

import { ServerConfigService, type ServerConfigUpdate } from "@/client"
import { ServerConfigUpdateSchema } from "@/client/schemas.gen"
import { CopyableValue } from "@/components/Common/CopyableValue"
import { LandingMarkdown } from "@/components/Landing/LandingMarkdown"
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
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

/**
 * The server's own cap, read off the generated schema rather than retyped.
 *
 * `LANDING_MARKDOWN_MAX_LENGTH` lives on `ServerConfigUpdate` in the backend
 * and is the only authority — it is what refuses an oversized payload. Copying
 * the number here would let the two drift, and the symptom would be a generic
 * "Failed to update" toast that never mentions length. The schema is
 * regenerated with the client, so this follows the backend automatically, and
 * a change to the field's shape breaks the build instead of going unnoticed.
 */
const LANDING_MARKDOWN_MAX_LENGTH =
  ServerConfigUpdateSchema.properties.landing_markdown.anyOf[0].maxLength

/**
 * The `/start` landing page is a frontend route served by the SPA, so the
 * address to paste into a wiki is built from the page origin — not from
 * `resolveServerOrigin`, which resolves the *API* origin and differs behind a
 * reverse proxy. Same choice, for the same reason, as `localAgentKitStartUrl`.
 */
function landingPageUrl(): string {
  return `${window.location.origin}/start`
}

/**
 * The public welcome message shown on `/start`, and the URL to share.
 *
 * Shares the `["serverConfig"]` query and the single partial-update endpoint
 * with the other cards on this page. It also invalidates `["landingPage"]` —
 * the anonymous projection this card just rewrote, cached for 60s by
 * `useLandingPage` — because without it `/start` would keep serving the old
 * copy in the admin's own session, with no error and no way to tell.
 */
export function LandingPageCard() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [editOpen, setEditOpen] = useState(false)
  const [draftMarkdown, setDraftMarkdown] = useState("")

  const { data: config, isLoading } = useQuery({
    queryKey: ["serverConfig"],
    queryFn: () => ServerConfigService.getServerConfig(),
  })

  const updateMutation = useMutation({
    mutationFn: (data: ServerConfigUpdate) =>
      ServerConfigService.updateServerConfig({ requestBody: data }),
    onSuccess: (updated) => {
      // Publish the returned row before the refetch, so the card does not
      // visibly snap back to the stale cached value under a success toast.
      queryClient.setQueryData(["serverConfig"], updated)
      queryClient.invalidateQueries({ queryKey: ["serverConfig"] })
      queryClient.invalidateQueries({ queryKey: ["landingPage"] })
    },
    // The shared extractor, not a fixed string: the server refuses this field
    // for a reason it spells out (over the length cap, or HTML-shaped copy —
    // see `ServerConfigUpdate.landing_markdown`), and a generic "failed"
    // toast is how an admin ends up retrying the same paste. `handleError`
    // already unwraps a 422's `detail[0].msg`.
    onError: handleError.bind(showErrorToast),
  })

  const markdown = config?.landing_markdown ?? ""
  // `""` with no value in hand is indistinguishable from "the admin never
  // wrote one", and here that is not a cosmetic difference: the effect below
  // would seed an empty draft, and `""` is a *meaningful* value on this
  // endpoint — saving it clears the column. So editing is gated on actually
  // holding the value.
  //
  // Gated on the value itself rather than on `!isLoading && !isError`, which
  // reasons about two of the *three* ways `data` can be undefined. With React
  // Query's default `networkMode: "online"`, a query that cannot start
  // because the browser is offline sits at `status: "pending"`,
  // `fetchStatus: "paused"` — `isLoading` false, `isError` false, `data`
  // undefined — and that spelling would open the form, seed `""` and wipe
  // live copy on reconnect. The same swap fixes the mirror case: a failed
  // *background* refetch reports `status: "error"` while the last good value
  // is still on screen, which must not deaden the button.
  const loaded = config !== undefined
  // Measured, never enforced by truncation: a `maxLength` on the textarea
  // silently swallows the tail of a pasted wiki page, and the admin finds out
  // when the landing page is missing half its copy. Refusing the save with the
  // count on screen is the honest version of the same limit.
  const overLimit = draftMarkdown.length > LANDING_MARKDOWN_MAX_LENGTH

  /**
   * Seeds the draft once, at the moment the dialog opens — deliberately not
   * from an effect keyed on the query result.
   *
   * An effect that reads `markdown` re-runs whenever `["serverConfig"]`
   * refetches, and this query refetches in the background on window focus and
   * after every save from the sibling cards on this page. With the dialog
   * open, that would replace whatever the admin has typed with the server's
   * copy mid-sentence, with nothing on screen to explain where their
   * paragraph went. Seeding at the opener instead gives the dialog sole
   * ownership of the draft for as long as it is open.
   *
   * The trade is the mirror case: if another admin saves a *different*
   * welcome message while this dialog is open, saving here overwrites theirs
   * without warning. That is accepted, for two reasons. It is what the
   * endpoint already is — `ServerConfigUpdate` is a field-wise partial merge
   * with no version or ETag to compare against, so a stale write is not
   * detectable from here even in principle without first adding a concurrency
   * token server-side; and the exposure is narrow, since only this one field
   * is sent, so a concurrent change to any *other* server setting is
   * untouched. Building conflict detection for a single-row, admin-only
   * settings card is out of proportion to the collision it guards against
   * (two admins editing the same welcome copy in the same minute), and the
   * two outcomes are not symmetric: a discarded remote edit is recoverable —
   * the value is one text box away — while the draft being typed right now is
   * gone for good.
   *
   * Gated on holding the value for the same reason the Edit button is (see
   * `loaded` above): opening without it would seed `""`, and `""` saves as a
   * *cleared* welcome message rather than an untouched one. The guard lives
   * here, at the seam, so it holds for any future opener rather than only for
   * the disabled button.
   */
  const openEditor = () => {
    if (!loaded) return
    setDraftMarkdown(markdown)
    setEditOpen(true)
  }

  const handleSaveMessage = () => {
    updateMutation.mutate(
      { landing_markdown: draftMarkdown },
      {
        onSuccess: () => {
          showSuccessToast("Landing page message saved")
          setEditOpen(false)
        },
      },
    )
  }

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <CardTitle>Public landing page</CardTitle>
          <CardDescription>
            One stable address anyone can open without an account. It shows the
            sign-in methods this server allows, the desktop download, and the
            welcome message you write here.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <CopyableValue label="Landing page URL" value={landingPageUrl()} />
          <p className="text-xs text-muted-foreground">
            Paste this into your internal wiki.
          </p>

          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium">Welcome message</p>
              {/* Keyed on the same `loaded` as the button: the caption must
                  never claim "no welcome message set" next to an Edit that
                  would save that claim. */}
              <p className="text-xs text-muted-foreground">
                {isLoading
                  ? "Loading the current message…"
                  : !loaded
                    ? "Could not load the current message — refresh to try again"
                    : markdown.trim()
                      ? "Edit the message shown above the sign-in options"
                      : "No welcome message set — the page omits the block"}
              </p>
            </div>
            <Button
              size="sm"
              variant="outline"
              onClick={openEditor}
              disabled={!loaded}
            >
              <FileText className="h-4 w-4 mr-1.5" />
              Edit
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Every way the dialog can open routes through `openEditor`, so the
          seeding and the `loaded` guard cannot be bypassed by adding a
          `DialogTrigger` later. Radix only ever reports `false` here today —
          Esc and the overlay click — but wiring it as a passthrough to
          `setEditOpen` would make that an assumption rather than an
          invariant. */}
      <Dialog
        open={editOpen}
        onOpenChange={(open) => (open ? openEditor() : setEditOpen(false))}
      >
        <DialogContent className="sm:max-w-[760px]">
          <DialogHeader>
            <DialogTitle>Edit Welcome Message</DialogTitle>
            <DialogDescription>
              Write the message in Markdown. Anyone who opens the landing page
              sees it, signed in or not — so keep internal details out of it.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-2 md:grid-cols-2">
            <div className="grid gap-2">
              <Label htmlFor="landing-markdown">Markdown</Label>
              <Textarea
                id="landing-markdown"
                value={draftMarkdown}
                onChange={(e) => setDraftMarkdown(e.target.value)}
                aria-invalid={overLimit}
                aria-describedby={
                  overLimit ? "landing-markdown-limit" : undefined
                }
                placeholder="# Welcome to Acme AI&#10;&#10;Use your @acme.com Google account to sign in."
                className="min-h-[320px] font-mono text-sm"
              />
              {overLimit && (
                <p
                  id="landing-markdown-limit"
                  className="text-xs text-destructive"
                >
                  {draftMarkdown.length.toLocaleString()} characters — the limit
                  is {LANDING_MARKDOWN_MAX_LENGTH.toLocaleString()}. Shorten it
                  to save.
                </p>
              )}
            </div>
            <div className="grid gap-2">
              <Label>Preview</Label>
              <div className="min-h-[320px] rounded-md border p-3 overflow-y-auto max-h-[320px] prose prose-sm dark:prose-invert max-w-none">
                {draftMarkdown.trim() ? (
                  // Deliberately the public `/start` renderer, not the chat
                  // one: this preview and the published page must stay one
                  // pipeline, or the two can silently drift apart.
                  <LandingMarkdown
                    content={draftMarkdown}
                    className="[&>h1:first-child]:!mt-0 [&>h2:first-child]:!mt-0 [&>h3:first-child]:!mt-0 [&>h4:first-child]:!mt-0"
                  />
                ) : (
                  <p className="text-sm text-muted-foreground">
                    Nothing to preview yet.
                  </p>
                )}
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleSaveMessage}
              disabled={updateMutation.isPending || overLimit}
            >
              {updateMutation.isPending ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
