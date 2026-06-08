/**
 * Open the MCP provider (OAuth/DCR) authorization URL in a popup and resolve
 * the outcome via ``postMessage`` from ``/mcp-providers/oauth/callback``.
 *
 * Mirrors the Google credential OAuth popup: the callback route posts back a
 * ``mcp_provider_oauth_success`` / ``mcp_provider_oauth_error`` message (origin
 * checked) and closes itself. We register a one-shot listener and clean it up
 * on the first matching message or when the popup is closed manually.
 */
interface OpenMcpProviderOAuthPopupArgs {
  authorizeUrl: string
  onSuccess: (credentialId?: string) => void
  onError: (message?: string) => void
}

export function openMcpProviderOAuthPopup({
  authorizeUrl,
  onSuccess,
  onError,
}: OpenMcpProviderOAuthPopupArgs): void {
  const width = 520
  const height = 680
  const left = window.screenX + (window.outerWidth - width) / 2
  const top = window.screenY + (window.outerHeight - height) / 2

  const popup = window.open(
    authorizeUrl,
    "mcp_provider_oauth",
    `width=${width},height=${height},left=${left},top=${top}`,
  )

  if (!popup) {
    onError("Popup blocked — allow popups and try Authorize again.")
    return
  }

  let settled = false

  const cleanup = () => {
    window.removeEventListener("message", handleMessage)
    window.clearInterval(pollClosed)
  }

  const handleMessage = (event: MessageEvent) => {
    if (event.origin !== window.location.origin) return
    const data = event.data as {
      type?: string
      credentialId?: string
      error?: string
    }
    if (data?.type === "mcp_provider_oauth_success") {
      settled = true
      cleanup()
      onSuccess(data.credentialId)
    } else if (data?.type === "mcp_provider_oauth_error") {
      settled = true
      cleanup()
      onError(data.error)
    }
  }

  window.addEventListener("message", handleMessage)

  // If the user closes the popup without finishing, surface a soft error once.
  const pollClosed = window.setInterval(() => {
    if (popup.closed) {
      window.clearInterval(pollClosed)
      if (!settled) {
        cleanup()
        onError("Authorization window was closed before completing.")
      }
    }
  }, 700)
}
