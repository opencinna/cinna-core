import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  createRootRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router"
import { createRoot } from "react-dom/client"

import { OpenAPI } from "@/client"
import { Route as CallbackRoute } from "@/routes/credentials/oauth/callback"
import "@/index.css"

// The production callback route under a bare root. The browser tests answer
// the API call themselves. Not wrapped in StrictMode: in dev, its doubled
// mount effect fires the single-use mutate before the MutationObserver
// re-subscribes, so the page never leaves "Completing authorization..."
// (production does not double-invoke effects).
OpenAPI.BASE = window.location.origin

const rootRoute = createRootRoute({ component: () => <Outlet /> })
const callbackRoute = CallbackRoute.update({
  id: "/credentials/oauth/callback",
  path: "/credentials/oauth/callback",
  getParentRoute: () => rootRoute,
} as Parameters<typeof CallbackRoute.update>[0])
const router = createRouter({
  routeTree: rootRoute.addChildren([callbackRoute]),
})
const queryClient = new QueryClient()
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={queryClient}>
    <RouterProvider router={router} />
  </QueryClientProvider>,
)
