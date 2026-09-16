import { AlertTriangle } from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"

/**
 * Shown when an Anthropic key field holds a Claude Code OAuth token
 * (`sk-ant-oat…`, from `claude setup-token`). Informational only — saving is
 * never blocked and such tokens still work, but the
 * subscription session behind them can be signed out without warning, which
 * silently breaks every cloud agent that uses the credential — so the platform
 * steers users to a Console API key instead.
 */
export function AnthropicOAuthTokenWarning() {
  return (
    <Alert>
      <AlertTriangle className="h-4 w-4" />
      <AlertDescription className="text-xs">
        This looks like a Claude Code OAuth token (
        <code className="font-mono">sk-ant-oat…</code>). You can save it, but
        these tokens can be signed out unexpectedly, which stops your cloud
        agents. We recommend an API key (
        <code className="font-mono">sk-ant-api…</code>) from{" "}
        <a
          href="https://console.anthropic.com/settings/keys"
          target="_blank"
          rel="noreferrer"
          className="text-primary underline underline-offset-2"
        >
          console.anthropic.com
        </a>{" "}
        instead.
      </AlertDescription>
    </Alert>
  )
}
