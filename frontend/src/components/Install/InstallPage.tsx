/**
 * InstallPage — single-screen replacement for the legacy InstallWizard.
 *
 * Layout:
 *   - Left column (lg+): sticky :class:`InstallAgentHeaderCard`.
 *   - Right column: scrollable :class:`InstallSetupForm` (AI section,
 *     per-spec service credential accordion, primary Install button).
 *   - On `md` and below the columns stack with the header on top.
 *
 * Uses :func:`useInstallContext` for the per-user install context
 * (auto-prefill suggestions + publisher AI summaries).
 */
import type { CatalogInstallContext } from "@/client"

import { InstallAgentHeaderCard } from "./InstallAgentHeaderCard"
import { InstallSetupForm } from "./InstallSetupForm"

interface InstallPageProps {
  context: CatalogInstallContext
}

export function InstallPage({ context }: InstallPageProps) {
  return (
    <div className="grid items-start gap-6 lg:grid-cols-[minmax(280px,360px)_1fr]">
      <div className="lg:order-1">
        <InstallAgentHeaderCard
          entry={context.bundle}
          serviceSpecs={context.service_specs}
        />
      </div>
      <div className="lg:order-2">
        <InstallSetupForm context={context} />
      </div>
    </div>
  )
}
