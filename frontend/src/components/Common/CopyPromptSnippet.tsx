import { Check, Copy } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"

/**
 * This instance's public Local Agent Kit entrypoint.
 *
 * Deliberately `window.location.origin`, not `VITE_API_URL`. `/agent-start` is the
 * *pasteable* URL: it is served at the SPA's own origin through an explicit
 * `location /agent-start` proxy block (see `frontend/nginx.conf` and
 * `docs/infrastructure/nginx_setup.md`), which is what makes it short enough for
 * a person to read aloud or type. `VITE_API_URL` points at the API host, which
 * on a split deployment is not where the pretty URL lives.
 *
 * The `/api/agent-start` alias — which every deployment already proxies via the
 * universal `/api/` rule — is the fallback the kit uses for its own internal
 * links, so an instance whose proxy was never updated still serves a working
 * kit. It is not what we show a human.
 */
export function localAgentKitStartUrl(): string {
  return `${window.location.origin}/agent-start`
}

/** The one line a user pastes into their coding assistant to get started. */
export function localAgentKitPrompt(
  startUrl: string = localAgentKitStartUrl(),
): string {
  return `read ${startUrl} and help me start making my agents`
}

interface CopyPromptSnippetProps {
  /** Override the computed `/agent-start` URL (tests, or an admin previewing a host). */
  startUrl?: string
  className?: string
}

/**
 * The starter prompt in a copyable block. Shared by the Getting Started
 * article, the admin Server Configuration card and the Local Development card,
 * so the wording a user pastes has exactly one source.
 */
export function CopyPromptSnippet({
  startUrl,
  className,
}: CopyPromptSnippetProps) {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [copied, setCopied] = useState(false)
  const prompt = localAgentKitPrompt(startUrl ?? localAgentKitStartUrl())

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(prompt)
      setCopied(true)
      showSuccessToast("Prompt copied — paste it into your coding assistant")
      setTimeout(() => setCopied(false), 2000)
    } catch {
      showErrorToast("Failed to copy the prompt")
    }
  }

  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-lg border bg-muted/50 p-3",
        className,
      )}
    >
      <code className="min-w-0 flex-1 break-all font-mono text-xs leading-relaxed">
        {prompt}
      </code>
      <Button
        type="button"
        variant="outline"
        size="icon"
        className="shrink-0"
        onClick={handleCopy}
        title="Copy prompt"
        aria-label="Copy the starter prompt"
      >
        {copied ? (
          <Check className="h-4 w-4 text-green-500" />
        ) : (
          <Copy className="h-4 w-4" />
        )}
      </Button>
    </div>
  )
}
