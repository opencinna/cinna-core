import { GoogleOAuthProvider } from "@react-oauth/google"
import {
  MutationCache,
  QueryCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query"
import { createRouter, RouterProvider } from "@tanstack/react-router"
import { StrictMode } from "react"
import ReactDOM from "react-dom/client"
import { ApiError, LoginService, OpenAPI } from "./client"
import NotFound from "./components/Common/NotFound"
import { ThemeProvider } from "./components/theme-provider"
import { Toaster } from "./components/ui/sonner"
import {
  bootstrapSession,
  clearSession,
  isRefreshInconclusive,
} from "./lib/authSession"
import {
  installAuthSession,
  isPublicPath,
  redirectToLogin,
} from "./lib/authSessionClient"
import "./index.css"
import { routeTree } from "./routeTree.gen"

OpenAPI.BASE = import.meta.env.VITE_API_URL
// Cookies on every request, the in-memory bearer, and the 401 → refresh →
// retry-once interceptor. No user JWT is ever read from Web Storage.
installAuthSession()

// A 401/403 does not necessarily mean "your session is dead". It also covers
// "you may not do this" (role gates like require_developer) and routes that
// surface a *third-party* auth failure — e.g. git versioning reporting that the
// remote host rejected the deploy key. Blanket-logging-out on the status code
// alone kicked users to /login mid-action for failures that had nothing to do
// with their token.
//
// So confirm with the dedicated token-validity endpoint before destroying the
// session. This is the same probe `useAuth.ensureSessionValid` uses. It fails
// safe in both directions: a genuinely invalid token fails the probe and still
// logs out, while a network/backend outage leaves the user signed in.
let sessionProbe: Promise<boolean> | null = null

// True when the backend still accepts our token. Concurrent failures share one
// in-flight probe so a page full of erroring queries issues a single request.
const isSessionStillValid = (): Promise<boolean> => {
  if (!sessionProbe) {
    sessionProbe = LoginService.testToken()
      .then(() => true)
      .catch((error: unknown) => {
        const status = (error as ApiError | null)?.status
        // A 401 after a refresh that failed transiently (5xx, network) says
        // nothing about the cookie session; clearing would orphan it.
        if (status === 401) return isRefreshInconclusive()
        // Only these mean the token itself is rejected. Anything else (network
        // failure, 5xx) is inconclusive — keep the user signed in.
        return !(status === 403 || status === 404)
      })
      .finally(() => {
        sessionProbe = null
      })
  }
  return sessionProbe
}

const handleApiError = async (error: Error) => {
  if (!(error instanceof ApiError) || ![401, 403].includes(error.status)) {
    return
  }
  // Don't redirect to login if we're on a guest share page, on a public
  // artifact link — a wrong link code answers 403 to a signed-out visitor —
  // or on a public skill link, which renders a stale token as signed out.
  if (isPublicPath()) return
  if (await isSessionStillValid()) return

  clearSession()
  // Preserve the current location as ?redirect= so consent/authorize
  // pages (OAuth MCP, Desktop Auth) bring the user back after re-login
  // instead of dropping them on the dashboard.
  redirectToLogin()
}
const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: handleApiError,
  }),
  mutationCache: new MutationCache({
    onError: handleApiError,
  }),
})

const router = createRouter({
  routeTree,
  // Catch unmatched URLs that fall through nested layout routes (e.g.
  // /sessions/<uuid>, which matches the `sessions` layout but has no child
  // route for the id). Without this, TanStack renders its raw built-in
  // "Not Found" text instead of our styled screen.
  // Fires for partial matches inside a layout (e.g. /sessions/<uuid> matches
  // the `sessions` layout but no child) — renders within the app shell, so use
  // the compact inline variant. The root route's notFoundComponent handles
  // fully unmatched top-level URLs full-screen.
  defaultNotFoundComponent: () => <NotFound inline />,
})
declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router
  }
}

const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID || ""

const AppTree = (
  <ThemeProvider defaultTheme="dark" storageKey="vite-ui-theme">
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
      <Toaster richColors closeButton />
    </QueryClientProvider>
  </ThemeProvider>
)

// S0: restore the session from the refresh cookie before the first render, so
// a reload lands signed in without a login flash. Capped at 5 s inside
// bootstrapSession; on timeout or any failure the app renders signed out.
void bootstrapSession()
  .catch(() => undefined)
  .then(() => {
    ReactDOM.createRoot(document.getElementById("root")!).render(
      <StrictMode>
        {GOOGLE_CLIENT_ID ? (
          <GoogleOAuthProvider clientId={GOOGLE_CLIENT_ID}>
            {AppTree}
          </GoogleOAuthProvider>
        ) : (
          AppTree
        )}
      </StrictMode>,
    )
  })
