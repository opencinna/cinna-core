import { createFileRoute, Link as RouterLink } from "@tanstack/react-router"
import { LogIn, Monitor, UserPlus } from "lucide-react"

import { DesktopDownloadSection } from "@/components/Desktop/DesktopDownloadSection"
import { LandingMarkdown } from "@/components/Landing/LandingMarkdown"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { googleSignInAvailable, useAccessPolicy } from "@/hooks/useAccessPolicy"
import { isLoggedIn } from "@/hooks/useAuth"
import { useLandingPage } from "@/hooks/useLandingPage"
import { useLocalAgentKitAvailable } from "@/hooks/useLocalAgentKit"
import { APP_NAME } from "@/utils"

/**
 * The instance's public front page — one stable URL an admin can paste into an
 * internal wiki, and the "just use the browser" link in the new-account email.
 *
 * Public by design and, like `/desktop`, with **no `beforeLoad`**: the visitor
 * has not signed in anywhere yet, so any session check that throws a redirect
 * (`ensureSessionValid`, which the consent routes use) would put a login wall
 * in front of the page whose entire job is to be reachable without one.
 *
 * `/start` never *states* the policy. It defers to `/login` and `/signup`,
 * which already own their own rendering and their own documented fallbacks —
 * see the reasoning on each element below.
 */
export const Route = createFileRoute("/start")({
  component: StartPage,
  head: () => ({
    meta: [{ title: `Get started - ${APP_NAME}` }],
  }),
})

function StartPage() {
  const { data: policy } = useAccessPolicy()
  const { data: landing } = useLandingPage()
  const localAgentKitAvailable = useLocalAgentKitAvailable()

  // A pure localStorage read, never a validation call: a returning visitor
  // gets a shortcut back to the app, and everyone else gets the page. A stale
  // token shows the banner and the `_layout` guard sorts it out on arrival —
  // strictly better than bouncing an anonymous visitor to `/login`.
  const signedIn = isLoggedIn()

  // `?? APP_NAME`, exactly as `accept-invite` reconciles the same two answers:
  // the projection's `project_name` is the configured instance name, and the
  // build-time `VITE_APP_NAME` is what every page title already uses. A slow
  // or failed policy read then shows the build's name rather than nothing.
  const projectName = policy?.project_name ?? APP_NAME
  const welcome = landing?.landing_markdown ?? ""

  // Positive answers only, on every policy-dependent element below.
  //
  // Loading, a 429, a network failure and a policy that says "no" all render
  // the same thing: nothing. That is the absence of an answer, not a second,
  // browser-side copy of the policy competing with the server's — and it is
  // available here in a way it is not on `/login` or `/signup`, because this
  // page is a hub rather than the only way in. "Log in" needs no policy fact
  // at all, so no failure can hide it.
  const passwordSignupAvailable = policy?.password_signup_available === true
  // "Google is the only way in" is `password_auth_enabled === false`, NOT
  // `password_signup_available === false`. The latter is false in two
  // different worlds — password sign-in off, and registration merely closed —
  // and on an invite-only instance that still accepts passwords it would tell
  // an anonymous visitor something untrue about how they sign in.
  const googleOnly =
    policy?.password_auth_enabled === false &&
    googleSignInAvailable(policy) === true
  const desktopOffered = policy?.desktop_enabled === true

  return (
    <div className="flex min-h-screen justify-center bg-background p-4">
      <div className="w-full max-w-xl space-y-6 py-10">
        <header className="space-y-2 text-center">
          <h1 className="text-3xl font-bold">{projectName}</h1>
          <p className="text-sm text-muted-foreground">
            Sign in to get started, or install the desktop app.
          </p>
        </header>

        {signedIn && (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-muted/40 px-4 py-3">
            <p className="text-sm text-muted-foreground">
              You&rsquo;re signed in on this browser.
            </p>
            <Button asChild size="sm" variant="secondary">
              <RouterLink to="/">Go to dashboard</RouterLink>
            </Button>
          </div>
        )}

        {/* `LandingMarkdown`, deliberately NOT the shared
            `Chat/MarkdownRenderer`: this is the one anonymous, unauthenticated
            consumer of admin-authored Markdown on the instance, and it gets a
            renderer whose plugin list nothing else can reach. See the comment
            in that file — it is the security boundary for this page.

            Guarded on `.trim()` before rendering, the way every other Markdown
            caller is: the renderer has no empty-input guard, so an unguarded
            call ships an empty bordered block to every visitor of an instance
            whose admin never wrote a welcome. */}
        {welcome.trim() && (
          <Card>
            <CardContent className="prose prose-sm dark:prose-invert max-w-none pt-6">
              <LandingMarkdown
                content={welcome}
                className="[&>h1:first-child]:!mt-0 [&>h2:first-child]:!mt-0 [&>h3:first-child]:!mt-0 [&>h4:first-child]:!mt-0"
              />
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <LogIn className="h-5 w-5" />
              Sign in
            </CardTitle>
            <CardDescription>
              Use your existing account, or create one if this server allows it.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {/* Unconditional: it depends on no policy fact, and `/login`
                renders whichever methods this instance actually offers. */}
            <Button asChild className="w-full">
              <RouterLink to="/login">Log in</RouterLink>
            </Button>

            {passwordSignupAvailable && (
              <Button asChild variant="outline" className="w-full">
                <RouterLink to="/signup">
                  <UserPlus className="mr-2 h-4 w-4" />
                  Create account
                </RouterLink>
              </Button>
            )}

            {googleOnly && (
              <p className="text-center text-xs text-muted-foreground">
                This server signs people in with Google. Continue from the
                log-in page.
              </p>
            )}
          </CardContent>
        </Card>

        {/* `desktop_enabled` gates the *advertisement* here and nothing else:
            `/desktop` stays reachable and the download resolver stays ungated,
            so the link in every already-sent new-account email keeps working. */}
        {desktopOffered && (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Monitor className="h-5 w-5" />
                Get Cinna Desktop
              </CardTitle>
              <CardDescription>
                Install the desktop app and sign in once — it sets itself up
                from there.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <DesktopDownloadSection />
            </CardContent>
          </Card>
        )}

        {/* Mirrors the login page's link, including the choice of the pretty
            `/agent-start` over the `/api/agent-start` alias the probe used —
            see the reasoning at `routes/login/index.tsx`. */}
        {localAgentKitAvailable && (
          <div className="text-center text-xs text-muted-foreground">
            <a
              href="/agent-start?format=html"
              target="_blank"
              rel="noopener"
              className="underline underline-offset-4 hover:text-foreground"
            >
              Building agents locally with Claude Code or Codex? Start here
            </a>
          </div>
        )}
      </div>
    </div>
  )
}
