import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { createRoot } from "react-dom/client"

import { OpenAPI } from "@/client"
import { DeviceLoginConsentPage } from "@/components/Auth/DeviceLoginConsentPage"
import { NativeAuthConsentPage } from "@/components/Auth/NativeAuthConsentPage"
import { AuthLayout } from "@/components/Common/AuthLayout"
import { ThemeProvider } from "@/components/theme-provider"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import "@/index.css"

// A split-host API base, as `main.tsx` sets it from VITE_API_URL: brand asset
// URLs must be prefixed with it. The browser tests answer every API call.
OpenAPI.BASE = "http://api.test"

// The production layout and consent page with their providers. The page body
// stands in for a sign-in form: a title, an input and a primary button.
function LayoutPage() {
  return (
    <AuthLayout>
      <form className="flex flex-col gap-4">
        <h1 className="text-2xl font-bold">Login to your account</h1>
        <Input aria-label="Email" placeholder="user@example.com" />
        <Button type="button">Log In</Button>
        <p className="text-sm text-muted-foreground">Forgot your password?</p>
      </form>
    </AuthLayout>
  )
}

function ConsentPage({
  branded,
  clientKind = "desktop",
}: {
  branded: boolean
  clientKind?: "desktop" | "mobile"
}) {
  return (
    <NativeAuthConsentPage
      nonce="ui-test"
      queryKeyPrefix="consent-test"
      branded={branded}
      getRequest={async () => ({
        device_name: "Ada's MacBook",
        platform: "darwin",
        app_version: "0.6.0",
        client_kind: clientKind,
      })}
      submitConsent={async () => ({ redirect_to: "about:blank" })}
    />
  )
}

const pages: Record<string, () => React.ReactNode> = {
  "/layout": () => <LayoutPage />,
  "/consent": () => <ConsentPage branded />,
  "/consent-unbranded": () => <ConsentPage branded={false} />,
  "/consent-mobile": () => <ConsentPage branded clientKind="mobile" />,
  // No code: the `cinna login` page opens on its code-entry form.
  "/device": () => <DeviceLoginConsentPage />,
}

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
})
createRoot(document.getElementById("root")!).render(
  <ThemeProvider defaultTheme="light" storageKey="vite-ui-theme">
    <QueryClientProvider client={queryClient}>
      {pages[window.location.pathname]?.()}
    </QueryClientProvider>
  </ThemeProvider>,
)
