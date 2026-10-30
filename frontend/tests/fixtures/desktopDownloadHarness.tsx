import { createRoot } from "react-dom/client"
import { Toaster } from "sonner"

import { DesktopDownloadSection } from "@/components/Desktop/DesktopDownloadSection"
import "@/index.css"

// Only the production section, at the width of the `/desktop` card
// (`max-w-md`, `p-6`). The browser tests stub the clipboard and answer the
// download resolver themselves; no backend is involved.
createRoot(document.getElementById("root")!).render(
  <>
    <div className="mx-auto w-full max-w-md p-6">
      <DesktopDownloadSection />
    </div>
    <Toaster />
  </>,
)
