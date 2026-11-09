import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { createRoot } from "react-dom/client"
import { Toaster } from "sonner"

import { OpenAPI } from "@/client"
import { BrandThemeCard } from "@/components/Admin/BrandTheme/BrandThemeCard"
import { BrandThemePreview } from "@/components/BrandTheme/BrandThemePreview"
import {
  type BrandMode,
  parseBrandThemePreview,
} from "@/components/BrandTheme/brandTheme"
import { ThemeProvider } from "@/components/theme-provider"
import "@/index.css"

// A split-host API base, as `main.tsx` sets it from VITE_API_URL. The browser
// tests answer every API call; no login, database or backend is involved.
OpenAPI.BASE = "http://api.test"

interface PreviewSpec {
  document: unknown
  mode: BrandMode
  name: string
}

declare global {
  interface Window {
    /** Set by the test before load: the previews `/previews` renders. */
    __previews?: PreviewSpec[]
  }
}

/** Several previews on one page, as the admin page can hold them. */
function Previews() {
  return (
    <div className="grid grid-cols-2 gap-4 p-4">
      {(window.__previews ?? []).map((spec, index) => (
        <BrandThemePreview
          key={index}
          theme={parseBrandThemePreview(spec.document)}
          mode={spec.mode}
          name={spec.name}
        />
      ))}
    </div>
  )
}

const pages: Record<string, () => React.ReactNode> = {
  // The Interface tab's card, in the tab's grid, without the route's auth
  // guard, page header and the sibling cards' own queries.
  "/admin/server-configuration": () => (
    <div className="grid grid-cols-1 gap-6 p-6 lg:grid-cols-2 items-start">
      <BrandThemeCard />
    </div>
  ),
  "/previews": () => <Previews />,
}

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
})
createRoot(document.getElementById("root")!).render(
  <ThemeProvider defaultTheme="light" storageKey="vite-ui-theme">
    <QueryClientProvider client={queryClient}>
      {pages[window.location.pathname]?.()}
      <Toaster />
    </QueryClientProvider>
  </ThemeProvider>,
)
