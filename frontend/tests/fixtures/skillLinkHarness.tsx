import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router"
import { createRoot } from "react-dom/client"
import { Toaster } from "sonner"

import { OpenAPI } from "@/client"
import { useCopySkillInstallLink } from "@/components/Catalog/useCopySkillInstallLink"
import { SkillLinkPage } from "@/components/SkillLink/SkillLinkPage"
import "@/index.css"

// The bearer token as `main.tsx` attaches it.
OpenAPI.TOKEN = async () => localStorage.getItem("access_token") || ""

// The production page and the production copy action, with their providers.
// The browser tests answer every API call themselves; no backend is involved.
const rootRoute = createRootRoute({
  component: () => (
    <>
      <Outlet />
      <Toaster />
    </>
  ),
})
const linkRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/get/skill/$packageId",
  component: function LinkRoute() {
    const { packageId } = linkRoute.useParams()
    return <SkillLinkPage packageId={packageId} />
  },
})
const copyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/copy/$packageId",
  component: function CopyRoute() {
    const { packageId } = copyRoute.useParams()
    const copy = useCopySkillInstallLink()
    return (
      <button type="button" onClick={() => copy(packageId)}>
        Copy install link
      </button>
    )
  },
})
const router = createRouter({
  routeTree: rootRoute.addChildren([linkRoute, copyRoute]),
})
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
})
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={queryClient}>
    <RouterProvider router={router} />
  </QueryClientProvider>,
)
