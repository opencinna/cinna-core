import { useMutation } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { Check, Loader2, X } from "lucide-react"
import { type ReactNode, useEffect, useRef } from "react"
import { z } from "zod"
import { CredentialsService } from "@/client"
import { getErrorMessage } from "@/utils"

// Schema for query parameters
const searchSchema = z.object({
  code: z.string().optional(),
  state: z.string().optional(),
  error: z.string().optional(),
})

export const Route = createFileRoute("/credentials/oauth/callback")({
  component: OAuthCallback,
  validateSearch: searchSchema,
})

/**
 * Desktop-initiated authorizations carry a `desktop.`-prefixed state, so the
 * page can pick its terminal view even when Google returns `?error=` and the
 * backend is never called. On success the response's `origin` is
 * authoritative. A desktop flow runs in the system browser: the tab cannot
 * close itself and there is no web session to redirect into, so the page is
 * the end state.
 *
 * The generated client sends the signed-in user's token when there is one;
 * the backend then refuses a state started by a different Cinna account.
 * Success shows the owning account either way.
 */
const DESKTOP_STATE_PREFIX = "desktop."
const RETURN_TO_DESKTOP = "Return to Cinna Desktop and try again."

function googleErrorMessage(code: string): string {
  return code === "access_denied"
    ? "Google access was not granted."
    : `Google returned: ${code}`
}

function postToOpener(message: Record<string, unknown>, closeAfterMs: number) {
  try {
    window.opener.postMessage(message, window.location.origin)
  } catch (e) {
    console.error("Failed to post message to opener:", e)
  }
  setTimeout(() => {
    window.close()
  }, closeAfterMs)
}

function OAuthCallback() {
  const navigate = useNavigate()
  const search = Route.useSearch()
  const stateIsDesktop = search.state?.startsWith(DESKTOP_STATE_PREFIX) ?? false
  const hasParams = Boolean(search.code && search.state)

  const callbackMutation = useMutation({
    mutationFn: (params: { code: string; state: string }) =>
      CredentialsService.oauthCallback({
        requestBody: {
          code: params.code,
          state: params.state,
        },
      }),
    onSuccess: (data) => {
      if (data.origin === "desktop" || stateIsDesktop) return
      if (window.opener) {
        postToOpener(
          { type: "oauth_success", credentialId: data.credential_id },
          1000,
        )
      } else {
        navigate({ to: "/credentials" })
      }
    },
    onError: (error: Error) => {
      if (stateIsDesktop) return
      if (window.opener) {
        postToOpener({ type: "oauth_error", error: error.message }, 2000)
      } else {
        navigate({ to: "/credentials" })
      }
    },
  })

  // StrictMode runs mount effects twice in dev; the code is single-use, so
  // the second run must not POST it again.
  const handledRef = useRef(false)

  // biome-ignore lint/correctness/useExhaustiveDependencies: runs once on mount; the code is single-use, a re-run would replay it
  useEffect(() => {
    if (handledRef.current) return
    handledRef.current = true

    // Check for OAuth error from Google
    if (search.error) {
      console.error("OAuth error from Google:", search.error)
      if (stateIsDesktop) return
      if (window.opener) {
        postToOpener(
          {
            type: "oauth_error",
            error: `Authorization failed: ${search.error}`,
          },
          2000,
        )
      } else {
        navigate({ to: "/credentials" })
      }
      return
    }

    // Process OAuth callback if we have code and state
    if (search.code && search.state) {
      callbackMutation.mutate({
        code: search.code,
        state: search.state,
      })
      return
    }

    // Missing required parameters
    console.error("Missing code or state parameter")
    if (stateIsDesktop) return
    if (window.opener) {
      postToOpener(
        {
          type: "oauth_error",
          error: "Missing authorization code or state parameter",
        },
        2000,
      )
    } else {
      navigate({ to: "/credentials" })
    }
  }, [])

  const webFollowUp = window.opener
    ? "You can close this window now."
    : "Redirecting..."

  // Exactly one block renders: Google's `?error=` wins, then a missing
  // code/state, then the mutation's state.
  let content: ReactNode = null
  if (search.error) {
    content = stateIsDesktop ? (
      <FailureBlock
        title="Authorization cancelled"
        lines={[googleErrorMessage(search.error), RETURN_TO_DESKTOP]}
      />
    ) : (
      <FailureBlock
        title="Authorization cancelled"
        lines={[search.error, webFollowUp]}
      />
    )
  } else if (!hasParams) {
    // Web: the popup closes / the page redirects with nothing drawn (unchanged).
    if (stateIsDesktop) {
      content = (
        <FailureBlock
          title="Authorization failed"
          lines={["The authorization link is incomplete.", RETURN_TO_DESKTOP]}
        />
      )
    }
  } else if (callbackMutation.isPending) {
    content = (
      <>
        <Loader2 className="h-8 w-8 animate-spin mx-auto" />
        <p className="text-lg font-medium">Completing authorization...</p>
        <p className="text-sm text-muted-foreground">
          Please wait while we connect your account
        </p>
      </>
    )
  } else if (callbackMutation.isSuccess) {
    const { data } = callbackMutation
    const desktop = data.origin === "desktop" || stateIsDesktop
    const account = data.owner_email_masked ?? null
    content = desktop ? (
      <SuccessBlock
        title="Connected"
        account={account}
        line="You can return to Cinna Desktop and close this tab."
      />
    ) : (
      <SuccessBlock
        title="Authorization successful!"
        account={account}
        line={webFollowUp}
      />
    )
  } else if (callbackMutation.isError) {
    const message = getErrorMessage(callbackMutation.error, "An error occurred")
    content = (
      <FailureBlock
        title="Authorization failed"
        lines={[message, stateIsDesktop ? RETURN_TO_DESKTOP : webFollowUp]}
      />
    )
  }

  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="text-center space-y-4">{content}</div>
    </div>
  )
}

/**
 * `account` is the Cinna account the credential now belongs to (masked
 * email). It is shown so a user who completed an authorization link someone
 * else sent them notices that the account is not theirs.
 */
function SuccessBlock({
  title,
  line,
  account,
}: {
  title: string
  line: string
  account: string | null
}) {
  return (
    <>
      <div className="h-8 w-8 rounded-full bg-success/15 flex items-center justify-center mx-auto">
        <Check className="h-5 w-5 text-success" />
      </div>
      <p className="text-lg font-medium">{title}</p>
      {account && (
        <p className="text-sm">
          Connected to Cinna account{" "}
          <span className="font-medium">{account}</span>
        </p>
      )}
      <p className="text-sm text-muted-foreground">{line}</p>
    </>
  )
}

function FailureBlock({ title, lines }: { title: string; lines: string[] }) {
  return (
    <>
      <div className="h-8 w-8 rounded-full bg-destructive/15 flex items-center justify-center mx-auto">
        <X className="h-5 w-5 text-destructive" />
      </div>
      <p className="text-lg font-medium">{title}</p>
      {lines.map((line, i) => (
        <p key={i} className="text-sm text-muted-foreground">
          {line}
        </p>
      ))}
    </>
  )
}
