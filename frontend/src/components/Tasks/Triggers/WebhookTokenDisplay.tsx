import { useState } from "react"
import { Copy, Check, AlertTriangle } from "lucide-react"
import { Button } from "@/components/ui/button"

interface WebhookTokenDisplayProps {
  token: string
  webhookUrl: string
}

export function WebhookTokenDisplay({ token, webhookUrl }: WebhookTokenDisplayProps) {
  const [copiedToken, setCopiedToken] = useState(false)
  const [copiedUrl, setCopiedUrl] = useState(false)
  const [copiedCurl, setCopiedCurl] = useState(false)

  const curlCommand = `curl -X POST ${webhookUrl} \\
  -H "Authorization: Bearer ${token}" \\
  -H "Content-Type: application/json" \\
  -d '{"key": "value"}'`

  const copyToClipboard = async (
    text: string,
    setter: (v: boolean) => void,
  ) => {
    await navigator.clipboard.writeText(text)
    setter(true)
    setTimeout(() => setter(false), 2000)
  }

  return (
    <div className="space-y-3 rounded-lg border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-950/30 p-4">
      <div className="flex items-center gap-2 text-amber-700 dark:text-amber-300">
        <AlertTriangle className="h-4 w-4" />
        <span className="text-sm font-medium">
          Save this token now — it can't be retrieved later
        </span>
      </div>

      {/* Token */}
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Token</label>
        <div className="flex items-center gap-2">
          <code className="flex-1 rounded bg-background px-2 py-1.5 text-xs font-mono break-all border">
            {token}
          </code>
          <Button
            variant="outline"
            size="icon"
            className="h-8 w-8 shrink-0"
            onClick={() => copyToClipboard(token, setCopiedToken)}
          >
            {copiedToken ? (
              <Check className="h-3 w-3 text-green-500" />
            ) : (
              <Copy className="h-3 w-3" />
            )}
          </Button>
        </div>
      </div>

      {/* Webhook URL */}
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Webhook URL</label>
        <div className="flex items-center gap-2">
          <code className="flex-1 rounded bg-background px-2 py-1.5 text-xs font-mono break-all border">
            {webhookUrl}
          </code>
          <Button
            variant="outline"
            size="icon"
            className="h-8 w-8 shrink-0"
            onClick={() => copyToClipboard(webhookUrl, setCopiedUrl)}
          >
            {copiedUrl ? (
              <Check className="h-3 w-3 text-green-500" />
            ) : (
              <Copy className="h-3 w-3" />
            )}
          </Button>
        </div>
      </div>

      {/* Curl example */}
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Example cURL</label>
        <div className="flex items-start gap-2">
          <pre className="flex-1 rounded bg-background px-2 py-1.5 text-xs font-mono break-all border overflow-x-auto whitespace-pre-wrap">
            {curlCommand}
          </pre>
          <Button
            variant="outline"
            size="icon"
            className="h-8 w-8 shrink-0 mt-0.5"
            onClick={() => copyToClipboard(curlCommand, setCopiedCurl)}
          >
            {copiedCurl ? (
              <Check className="h-3 w-3 text-green-500" />
            ) : (
              <Copy className="h-3 w-3" />
            )}
          </Button>
        </div>
      </div>
    </div>
  )
}
