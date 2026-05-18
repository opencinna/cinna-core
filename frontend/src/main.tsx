import {
  MutationCache,
  QueryCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query"
import { createRouter, RouterProvider } from "@tanstack/react-router"
import { GoogleOAuthProvider } from "@react-oauth/google"
import { StrictMode } from "react"
import ReactDOM from "react-dom/client"
import { ApiError, OpenAPI } from "./client"
import { ThemeProvider } from "./components/theme-provider"
import { Toaster } from "./components/ui/sonner"
import { safeRedirectPath } from "./utils"
import "./index.css"
import { routeTree } from "./routeTree.gen"

OpenAPI.BASE = import.meta.env.VITE_API_URL
OpenAPI.TOKEN = async () => {
  return localStorage.getItem("access_token") || ""
}

const handleApiError = (error: Error) => {
  if (error instanceof ApiError && [401, 403].includes(error.status)) {
    // Don't redirect to login if we're on a guest share page
    if (window.location.pathname.startsWith("/guest/")) {
      return
    }
    localStorage.removeItem("access_token")
    // Preserve the current location as ?redirect= so consent/authorize
    // pages (OAuth MCP, Desktop Auth) bring the user back after re-login
    // instead of dropping them on the dashboard.
    const here =
      window.location.pathname + window.location.search + window.location.hash
    const safe = safeRedirectPath(here)
    if (safe !== "/" && safe !== "/login") {
      window.location.href = `/login?redirect=${encodeURIComponent(safe)}`
    } else {
      window.location.href = "/login"
    }
  }
}
const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: handleApiError,
  }),
  mutationCache: new MutationCache({
    onError: handleApiError,
  }),
})

const router = createRouter({ routeTree })
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
