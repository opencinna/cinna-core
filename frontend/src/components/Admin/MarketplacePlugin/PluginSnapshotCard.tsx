import { Archive } from "lucide-react"

import type { LLMPluginMarketplacePluginPublic } from "@/client"
import { formatBytes } from "@/components/Artifacts/artifactFormat"
import { StatusDot } from "@/components/Common/StatusDot"
import { Alert, AlertDescription } from "@/components/ui/alert"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { snapshotReasonSentence, snapshotStatusMeta } from "@/utils/marketplace"
import { CopyValueButton, Fact } from "./PluginFacts"

/** How many hex characters of the digest are shown; the full one is copied. */
const DIGEST_SHOWN = 12

/**
 * S2 — whether this entry can be delivered to a desktop and, if not, why.
 * The caller renders it only when the row has a snapshot state at all.
 */
export function PluginSnapshotCard({
  plugin,
}: {
  plugin: LLMPluginMarketplacePluginPublic
}) {
  const meta = snapshotStatusMeta(plugin.snapshot_status)
  if (!meta) return null
  const digest = plugin.archive_sha256 || null
  const size = plugin.archive_size ?? null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex min-w-0 items-center gap-2">
          <Archive className="h-5 w-5" />
          Snapshot
        </CardTitle>
        <CardDescription>
          The copy Cinna Desktop downloads when an agent adds this entry.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {meta.status === "failed" && (
          <Alert>
            <AlertDescription>
              {snapshotReasonSentence(plugin.snapshot_reason)}
            </AlertDescription>
          </Alert>
        )}
        <div className="grid grid-cols-2 gap-4">
          <Fact label="Status">
            <StatusDot tone={meta.tone} label={meta.label} />
            {meta.status === "pending" && (
              <p className="mt-1 text-xs text-muted-foreground">
                Fetching from its source — this page updates when it is ready.
              </p>
            )}
          </Fact>
          {digest && (
            <Fact label="Archive digest">
              <CopyValueButton
                value={digest}
                display={digest.slice(0, DIGEST_SHOWN)}
                noun="Digest"
              />
            </Fact>
          )}
          {size !== null && (
            <Fact label="Archive size">{formatBytes(size)}</Fact>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
