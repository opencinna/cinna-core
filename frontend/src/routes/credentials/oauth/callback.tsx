import { useMutation } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { Check, Loader2, X } from "lucide-react"
import { type ReactNode, useEffect, useRef } from "react"
import { z } from "zod"
import { CredentialsService } from "@/client"
import { Button } from "@/components/ui/button"
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
 * the end state. It hands the user back to the app through the `cinna://`
 * deep link: once automatically on success, and as an "Open Cinna Desktop"
 * link on every terminal desktop view (a click is a user gesture for
 * browsers that block a scripted custom-scheme navigation). The link carries
 * nothing secret; the desktop only treats it as a nudge to come to the front
 * and re-read its credential list.
 *
 * Mobile-initiated authorizations carry `mobile.<scheme>.<token>`, where the
 * scheme is the app's return scheme. It is read from the state (Google's
 * `?error=` path never reaches the backend) and honoured only when it fully
 * matches `cinna-mobile(-<suffix>)*`; anything else falls back to the plain
 * web view, so the page never navigates to an arbitrary scheme. The views
 * mirror the desktop ones: one automatic hand-back on success, and a
 * "Return to Cinna" link on every terminal mobile view.
 *
 * The generated client sends the signed-in user's token when there is one;
 * the backend then refuses a state started by a different Cinna account.
 * Success shows the owning account either way.
 */
const DESKTOP_STATE_PREFIX = "desktop."
const RETURN_TO_DESKTOP = "Return to Cinna Desktop and try again."

const DESKTOP_DONE_LINK = "cinna://oauth-done"
const desktopSuccessLink = (credentialId: string) =>
  `${DESKTOP_DONE_LINK}?credential_id=${encodeURIComponent(credentialId)}`
const DESKTOP_ERROR_LINK = `${DESKTOP_DONE_LINK}?status=error`

const MOBILE_STATE_PREFIX = "mobile."
const MOBILE_SCHEME_RE = /^cinna-mobile(-[a-z0-9]+)*$/
const RETURN_TO_MOBILE = "Return to the Cinna app and try again."
const MOBILE_LINK_LABEL = "Return to Cinna"

/** The app's return scheme from a `mobile.<scheme>.<token>` state, or null. */
function mobileReturnScheme(state?: string): string | null {
  if (!state?.startsWith(MOBILE_STATE_PREFIX)) return null
  const rest = state.slice(MOBILE_STATE_PREFIX.length)
  const end = rest.indexOf(".")
  if (end <= 0) return null
  const scheme = rest.slice(0, end)
  return MOBILE_SCHEME_RE.test(scheme) ? scheme : null
}
const mobileSuccessLink = (scheme: string, credentialId: string) =>
  `${scheme}://oauth-done?credential_id=${encodeURIComponent(credentialId)}`
const mobileErrorLink = (scheme: string) =>
  `${scheme}://oauth-done?status=error`

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
  const mobileScheme = stateIsDesktop ? null : mobileReturnScheme(search.state)
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
      if (data.origin === "desktop" || stateIsDesktop || mobileScheme) return
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
      if (stateIsDesktop || mobileScheme) return
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
      if (stateIsDesktop || mobileScheme) return
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
    if (stateIsDesktop || mobileScheme) return
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

  // Hand the browser back to the app once, on success only. A failure
  // leaves the user on the page to read why; its link is a click away.
  const desktopSuccess =
    callbackMutation.isSuccess &&
    (callbackMutation.data.origin === "desktop" || stateIsDesktop)
  const successLink = desktopSuccess
    ? desktopSuccessLink(callbackMutation.data.credential_id)
    : callbackMutation.isSuccess && mobileScheme
      ? mobileSuccessLink(mobileScheme, callbackMutation.data.credential_id)
      : null
  const openedDesktopRef = useRef(false)
  useEffect(() => {
    if (!successLink || openedDesktopRef.current) return
    openedDesktopRef.current = true
    window.location.href = successLink
  }, [successLink])

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
        desktopLink={DESKTOP_ERROR_LINK}
      />
    ) : mobileScheme ? (
      <FailureBlock
        title="Authorization cancelled"
        lines={[googleErrorMessage(search.error), RETURN_TO_MOBILE]}
        desktopLink={mobileErrorLink(mobileScheme)}
        linkLabel={MOBILE_LINK_LABEL}
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
          desktopLink={DESKTOP_ERROR_LINK}
        />
      )
    } else if (mobileScheme) {
      content = (
        <FailureBlock
          title="Authorization failed"
          lines={["The authorization link is incomplete.", RETURN_TO_MOBILE]}
          desktopLink={mobileErrorLink(mobileScheme)}
          linkLabel={MOBILE_LINK_LABEL}
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
    const account = callbackMutation.data.owner_email_masked ?? null
    content =
      desktopSuccess && successLink ? (
        <SuccessBlock
          title="Connected"
          account={account}
          line="You can close this tab."
          desktopLink={successLink}
        />
      ) : successLink ? (
        <SuccessBlock
          title="Connected"
          account={account}
          line="You can return to the Cinna app."
          desktopLink={successLink}
          linkLabel={MOBILE_LINK_LABEL}
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
    content = mobileScheme ? (
      <FailureBlock
        title="Authorization failed"
        lines={[message, RETURN_TO_MOBILE]}
        desktopLink={mobileErrorLink(mobileScheme)}
        linkLabel={MOBILE_LINK_LABEL}
      />
    ) : (
      <FailureBlock
        title="Authorization failed"
        lines={[message, stateIsDesktop ? RETURN_TO_DESKTOP : webFollowUp]}
        desktopLink={stateIsDesktop ? DESKTOP_ERROR_LINK : undefined}
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
  desktopLink,
  linkLabel,
}: {
  title: string
  line: string
  account: string | null
  desktopLink?: string
  linkLabel?: string
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
      {desktopLink && <OpenDesktopLink href={desktopLink} label={linkLabel} />}
      <p className="text-sm text-muted-foreground">{line}</p>
    </>
  )
}

function FailureBlock({
  title,
  lines,
  desktopLink,
  linkLabel,
}: {
  title: string
  lines: string[]
  desktopLink?: string
  linkLabel?: string
}) {
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
      {desktopLink && <OpenDesktopLink href={desktopLink} label={linkLabel} />}
    </>
  )
}

function OpenDesktopLink({
  href,
  label = "Open Cinna Desktop",
}: {
  href: string
  label?: string
}) {
  return (
    <Button asChild>
      <a href={href}>{label}</a>
    </Button>
  )
}
