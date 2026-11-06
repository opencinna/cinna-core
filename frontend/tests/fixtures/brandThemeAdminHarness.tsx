import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { createRoot } from "react-dom/client"
import { Toaster } from "sonner"

import { OpenAPI } from "@/client"
import { BrandThemeAdmin } from "@/components/Admin/BrandTheme/BrandThemeAdmin"
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
  // The route's body, without the route's auth guard and page header.
  "/admin": () => (
    <div className="p-6">
      <BrandThemeAdmin />
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
