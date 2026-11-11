import { ChartColumn } from "lucide-react"
import { useState } from "react"

import { useServerConfig } from "@/components/Admin/AccessPolicy/accessPolicy"
import { PreviewList } from "@/components/Common/PreviewList"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { AllVoiceUsageSheet } from "./AllVoiceUsageSheet"
import { VoiceUsageRow } from "./VoiceUsageRow"
import {
  formatMinutes,
  formatSessions,
  useVoiceUsage,
} from "./voiceTranscription"

/**
 * Who dictated how much this month, and whether anyone hit the limit.
 *
 * Read-only: five rows in API order (most seconds first) and the rest in a
 * Sheet. The period is stated in words; no wire timestamp is rendered.
 */
export function VoiceUsageCard() {
  const [showAll, setShowAll] = useState(false)
  const usage = useVoiceUsage()
  const { data: config } = useServerConfig()

  const report = usage.data
  const rows = report?.rows ?? []
  const enabled = config?.voice_transcription_enabled === true
  const limitMinutes =
    config?.voice_transcription_monthly_minutes_per_user ?? null

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <ChartColumn className="h-5 w-5 shrink-0" />
          Voice usage
        </CardTitle>
        <CardDescription>
          Dictation this month, most minutes first. Resets on the 1st (UTC).
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {report && rows.length > 0 && (
          <p className="text-sm">
            {formatMinutes(report.total_seconds)} ·{" "}
            {formatSessions(report.total_sessions)} · {rows.length}{" "}
            {rows.length === 1 ? "person" : "people"}
          </p>
        )}
        <PreviewList
          items={rows}
          getKey={(row) => row.user_id}
          renderItem={(row) => (
            <VoiceUsageRow row={row} limitMinutes={limitMinutes} />
          )}
          isLoading={report === undefined && !usage.isError}
          isError={usage.isError && report === undefined}
          error={usage.error}
          onRetry={() => void usage.refetch()}
          errorFallback="Couldn't load voice usage."
          skeletonClassName="h-[40px] w-full rounded-md"
          empty={
            <p className="text-sm text-muted-foreground">
              {enabled || config === undefined
                ? "No one has dictated this month."
                : "Nothing is counted while voice transcription is off."}
            </p>
          }
          onShowAll={() => setShowAll(true)}
        />
      </CardContent>
      {report && (
        <AllVoiceUsageSheet
          report={report}
          limitMinutes={limitMinutes}
          open={showAll}
          onOpenChange={setShowAll}
        />
      )}
    </Card>
  )
}
