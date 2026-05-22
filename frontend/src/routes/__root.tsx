// import { ReactQueryDevtools } from "@tanstack/react-query-devtools"
import { createRootRoute, HeadContent, Outlet } from "@tanstack/react-router"
// import { TanStackRouterDevtools } from "@tanstack/react-router-devtools"
import { MfaChallengeProvider } from "@/components/Auth/MfaChallengeContext"
import ErrorComponent from "@/components/Common/ErrorComponent"
import NotFound from "@/components/Common/NotFound"
import { WorkspaceProvider } from "@/hooks/useWorkspace"

export const Route = createRootRoute({
  component: () => (
    <>
      <HeadContent />
      <MfaChallengeProvider>
        <WorkspaceProvider>
          <Outlet />
        </WorkspaceProvider>
      </MfaChallengeProvider>
      {/*<TanStackRouterDevtools position="bottom-right" />*/}
      {/*<ReactQueryDevtools initialIsOpen={false} />*/}
    </>
  ),
  notFoundComponent: () => <NotFound />,
  errorComponent: () => <ErrorComponent />,
})
