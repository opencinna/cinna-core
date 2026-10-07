import { useMutation } from "@tanstack/react-query"
import { Check, Copy, Eye, EyeOff, FileJson } from "lucide-react"
import { useEffect, useState } from "react"

import { CredentialsService } from "@/client"
import { buildAgentApiKeyCurl } from "@/components/Credentials/agentApiKeyCopy"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"

interface AgentApiKeyUsageProps {
  credentialId: string
  /** The key's public prefix — what the masked placeholder shows. */
  tokenPrefix: string
  /** PUBLIC base URL (never the container-rewritten one). */
  baseUrl: string
  /** Revealed value, owned by the card so a rotate can show its new value. */
  token: string | null
  onTokenChange: (token: string | null) => void
  /** Opens the holder's spec; absent once the producer is gone. */
  onViewSpec?: () => void
}

/**
 * The value, its base URL, and a runnable curl.
 *
 * The value is masked until the user asks for it, and asking FETCHES it —
 * ``POST /credentials/{id}/agent-api-key/reveal``, the one path that returns a
 * key after issue and the one that writes ``AGENT_API_EXTERNAL_KEY_REVEALED``.
 * Two consequences shape this component:
 *
 * - It is a **mutation, not a query**. A query would re-run on remount and
 *   window refocus, and every one of those would be a security event for a
 *   disclosure nobody asked for. One click, one call, one event.
 * - Hiding **drops** the value rather than keeping it behind a display toggle,
 *   so the next Reveal is a real, audited disclosure too.
 *
 * Two reveals do NOT call the endpoint: the post-issue one (the value comes
 * from the issue response via ``agentApiKeyMintHandoff``) and the post-rotate
 * one (the rotate response carries the new value). Both are values the user
 * was just handed; auditing them would be noise.
 */
export function AgentApiKeyUsage({
  credentialId,
  tokenPrefix,
  baseUrl,
  token,
  onTokenChange,
  onViewSpec,
}: AgentApiKeyUsageProps) {
  // Reported inline rather than as a toast: the failure belongs to the control
  // the user just pressed, and a toast would be gone by the time they look for
  // the value that never appeared.
  const revealMutation = useMutation({
    mutationFn: () => CredentialsService.revealAgentApiKey({ id: credentialId }),
    onSuccess: (data) => onTokenChange(data.token),
    // Never keep the revealed value in the MutationCache after this unmounts.
    gcTime: 0,
  })

  // Copy must work whether or not the value is on screen, so when it is hidden
  // this fetches it just for the clipboard write. Deliberately NOT
  // `revealMutation.mutateAsync`: that would run its `onSuccess` and un-hide
  // the field, turning a copy into a reveal. The audit event still fires,
  // which is right: copying is a disclosure.
  const resolveToken = async () =>
    token ??
    (await CredentialsService.revealAgentApiKey({ id: credentialId })).token

  const revealed = token !== null
  const masked = `${tokenPrefix}${"•".repeat(24)}`

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <Label className="text-xs text-muted-foreground">Key</Label>
        <div className="flex items-center gap-2">
          {/* Wraps once revealed: truncating the one thing the page exists to
              hand over would make it unreadable without the copy button. */}
          <code
            className={`flex-1 min-w-0 rounded-md border bg-muted/40 px-3 py-2 font-mono text-xs ${
              revealed ? "break-all" : "truncate"
            }`}
          >
            {token ?? masked}
          </code>
          <LoadingButton
            variant="outline"
            size="sm"
            className="shrink-0"
            loading={revealMutation.isPending}
            onClick={() => {
              if (!revealed) {
                revealMutation.mutate()
                return
              }
              // Drop the value AND the mutation's copy of it, so "hidden"
              // means the browser is not holding the key anywhere.
              onTokenChange(null)
              revealMutation.reset()
            }}
          >
            {revealMutation.isPending ? null : revealed ? (
              <EyeOff className="h-4 w-4 mr-1.5" />
            ) : (
              <Eye className="h-4 w-4 mr-1.5" />
            )}
            {revealed ? "Hide" : "Reveal"}
          </LoadingButton>
          <CopyButton value={token} label="Copy key" resolve={resolveToken} />
        </div>
        {revealMutation.isError && (
          <p className="text-xs text-destructive">
            {getErrorMessage(
              revealMutation.error,
              "Could not reveal the key. Try again.",
            )}
          </p>
        )}
        <p className="text-xs text-muted-foreground">
          Treat it like a password: whoever holds it calls this API as you.
          Every reveal is recorded as a security event.
        </p>
      </div>

      <div className="space-y-2">
        <Label className="text-xs text-muted-foreground">Base URL</Label>
        <div className="flex items-center gap-2">
          <code className="flex-1 min-w-0 truncate rounded-md border bg-muted/40 px-3 py-2 font-mono text-xs">
            {baseUrl || "—"}
          </code>
          <CopyButton value={baseUrl || null} label="Copy base URL" />
          {onViewSpec && (
            <Button
              variant="outline"
              size="sm"
              className="shrink-0"
              onClick={onViewSpec}
            >
              <FileJson className="h-4 w-4 mr-1.5" />
              View spec
            </Button>
          )}
        </div>
      </div>

      <div className="space-y-2">
        <div className="flex items-center justify-between gap-2">
          <Label className="text-xs text-muted-foreground">Try it</Label>
          {/* Always copies a RUNNABLE curl, masked or not. `value` is null
              while hidden precisely so the resolve path runs: the builder
              always returns a string, so passing it unconditionally would
              short-circuit `value ?? resolve()` and copy the placeholder. */}
          <CopyButton
            value={token ? buildAgentApiKeyCurl(baseUrl, token) : null}
            label="Copy curl"
            resolve={async () =>
              buildAgentApiKeyCurl(baseUrl, await resolveToken())
            }
          />
        </div>
        <pre className="overflow-x-auto rounded-md border bg-muted/40 px-3 py-2 font-mono text-xs">
          {buildAgentApiKeyCurl(baseUrl, token)}
        </pre>
      </div>
    </div>
  )
}

/** Icon-only copy button with a tooltip and a short confirmation state. */
function CopyButton({
  value,
  label,
  resolve,
}: {
  value: string | null
  label: string
  /**
   * Supplies the value when it is not on screen, through the audited reveal
   * endpoint. The resolved value is used for the clipboard write only and is
   * never stored, so copying does not un-hide the field.
   */
  resolve?: () => Promise<string | null>
}) {
  const [copied, setCopied] = useState(false)
  const [busy, setBusy] = useState(false)
  const { showErrorToast } = useCustomToast()

  useEffect(() => {
    if (!copied) return
    const handle = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(handle)
  }, [copied])

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="shrink-0"
          disabled={(!value && !resolve) || busy}
          aria-label={label}
          onClick={async () => {
            setBusy(true)
            try {
              // Resolving and writing are reported separately: a failed
              // resolve is a server error, and blaming the clipboard for it
              // sends the user looking in the wrong place.
              let resolved = value
              if (!resolved && resolve) {
                try {
                  resolved = await resolve()
                } catch (e) {
                  showErrorToast(
                    getErrorMessage(e, "Could not retrieve the value to copy."),
                  )
                  return
                }
              }
              if (!resolved) return
              await navigator.clipboard.writeText(resolved)
              setCopied(true)
            } catch {
              showErrorToast("Could not copy to clipboard")
            } finally {
              setBusy(false)
            }
          }}
        >
          {copied ? (
            <Check className="h-4 w-4 text-success" />
          ) : (
            <Copy className="h-4 w-4" />
          )}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs">
        {copied ? "Copied" : label}
      </TooltipContent>
    </Tooltip>
  )
}
