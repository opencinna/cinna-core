import { useMutation } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { Loader2 } from "lucide-react"
import { useEffect } from "react"
import { z } from "zod"

import { McpProvidersService } from "@/client"

// Authorization-code callback for MCP provider (OAuth/DCR) credentials.
// The target authorization server redirects the browser here with
// (code, state); we forward them to POST /mcp-providers/oauth/callback and
// signal the opener window (the "Connect MCP Provider" flow runs the
// authorization in a popup, mirroring the Google credential OAuth callback).
const searchSchema = z.object({
  code: z.string().optional(),
  state: z.string().optional(),
  error: z.string().optional(),
})

export const Route = createFileRoute("/mcp-providers/oauth/callback")({
  component: McpProviderOAuthCallback,
  validateSearch: searchSchema,
})

function McpProviderOAuthCallback() {
  const navigate = useNavigate()
  const search = Route.useSearch()

  const callbackMutation = useMutation({
    mutationFn: (params: { code: string; state: string }) =>
      McpProvidersService.oauthCallback({
        requestBody: { code: params.code, state: params.state },
      }),
    onSuccess: (data) => {
      if (window.opener) {
        try {
          window.opener.postMessage(
            {
              type: "mcp_provider_oauth_success",
              credentialId: data.credential_id,
            },
            window.location.origin,
          )
        } catch (e) {
          console.error("Failed to post message to opener:", e)
        }
        setTimeout(() => window.close(), 1000)
      } else {
        navigate({
          to: "/credential/$credentialId",
          params: { credentialId: data.credential_id },
        })
      }
    },
    onError: (error: Error) => {
      if (window.opener) {
        try {
          window.opener.postMessage(
            { type: "mcp_provider_oauth_error", error: error.message },
            window.location.origin,
          )
        } catch (e) {
          console.error("Failed to post message to opener:", e)
        }
        setTimeout(() => window.close(), 2000)
      } else {
        navigate({ to: "/credentials" })
      }
    },
  })

  useEffect(() => {
    if (search.error) {
      if (window.opener) {
        try {
          window.opener.postMessage(
            {
              type: "mcp_provider_oauth_error",
              error: `Authorization failed: ${search.error}`,
            },
            window.location.origin,
          )
        } catch (e) {
          console.error("Failed to post message to opener:", e)
        }
        setTimeout(() => window.close(), 2000)
      } else {
        navigate({ to: "/credentials" })
      }
      return
    }

    if (search.code && search.state) {
      callbackMutation.mutate({ code: search.code, state: search.state })
    } else {
      if (window.opener) {
        try {
          window.opener.postMessage(
            {
              type: "mcp_provider_oauth_error",
              error: "Missing authorization code or state parameter",
            },
            window.location.origin,
          )
        } catch (e) {
          console.error("Failed to post message to opener:", e)
        }
        setTimeout(() => window.close(), 2000)
      } else {
        navigate({ to: "/credentials" })
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="text-center space-y-4">
        {callbackMutation.isPending && (
          <>
            <Loader2 className="h-8 w-8 animate-spin mx-auto" />
            <p className="text-lg font-medium">Completing authorization...</p>
            <p className="text-sm text-muted-foreground">
              Please wait while we connect the MCP server
            </p>
          </>
        )}

        {callbackMutation.isSuccess && (
          <>
            <div className="h-8 w-8 rounded-full bg-green-100 dark:bg-green-900 flex items-center justify-center mx-auto">
              <svg
                className="h-5 w-5 text-green-600 dark:text-green-200"
                fill="none"
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="2"
                viewBox="0 0 24 24"
                stroke="currentColor"
              >
                <path d="M5 13l4 4L19 7" />
              </svg>
            </div>
            <p className="text-lg font-medium text-green-600 dark:text-green-200">
              Authorization successful!
            </p>
            <p className="text-sm text-muted-foreground">
              {window.opener
                ? "You can close this window now."
                : "Redirecting..."}
            </p>
          </>
        )}

        {(callbackMutation.isError || search.error) && (
          <>
            <div className="h-8 w-8 rounded-full bg-red-100 dark:bg-red-900 flex items-center justify-center mx-auto">
              <svg
                className="h-5 w-5 text-red-600 dark:text-red-200"
                fill="none"
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="2"
                viewBox="0 0 24 24"
                stroke="currentColor"
              >
                <path d="M6 18L18 6M6 6l12 12" />
              </svg>
            </div>
            <p className="text-lg font-medium text-red-600 dark:text-red-200">
              Authorization failed
            </p>
            <p className="text-sm text-muted-foreground">
              {callbackMutation.error?.message ||
                search.error ||
                "An error occurred"}
            </p>
            <p className="text-sm text-muted-foreground">
              {window.opener
                ? "You can close this window now."
                : "Redirecting..."}
            </p>
          </>
        )}
      </div>
    </div>
  )
}
