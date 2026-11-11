import { useQuery, useQueryClient } from "@tanstack/react-query"

import { VoiceTranscriptionService } from "@/client"
import { useServerConfigUpdate } from "@/components/Admin/AccessPolicy/accessPolicy"
import { parseAccessPolicyReason } from "@/utils/accessPolicyReasons"

/**
 * What the Features tab's two voice cards share: the query keys, the save hook
 * with this feature's words, and the one minutes formatter both the rows and
 * the totals use so they can never round differently.
 */

export const VOICE_PROVIDERS_QUERY_KEY = ["voiceTranscription", "providers"]
export const VOICE_USAGE_QUERY_KEY = ["voiceTranscription", "usage"]

/** The `PUT /admin/server-config` refusals this feature can cause. */
const SAVE_REASON_COPY: Record<string, string> = {
  voice_transcription_provider_required:
    "Choose a provider before turning voice transcription on.",
  voice_transcription_provider_not_found:
    "That provider no longer exists. Choose another.",
  voice_transcription_provider_not_speech_capable:
    "That provider can't transcribe. Choose a Google API-key provider.",
}

export function voiceSaveErrorMessage(error: unknown): string {
  const { code } = parseAccessPolicyReason(error)
  if (code && SAVE_REASON_COPY[code]) return SAVE_REASON_COPY[code]
  return code
    ? `Could not save voice transcription (${code}).`
    : "Could not save voice transcription."
}

/** Speech-capable providers an admin may pick (Google API-key providers). */
export function useVoiceProviders() {
  return useQuery({
    queryKey: VOICE_PROVIDERS_QUERY_KEY,
    queryFn: () => VoiceTranscriptionService.listVoiceTranscriptionProviders(),
  })
}

/** Per-person usage for the current UTC month, most seconds first. */
export function useVoiceUsage() {
  return useQuery({
    queryKey: VOICE_USAGE_QUERY_KEY,
    queryFn: () => VoiceTranscriptionService.getVoiceTranscriptionUsageReport(),
  })
}

/**
 * The shared server-config write, with this feature's toast copy.
 *
 * A successful save also refreshes the usage card: its empty sentence depends
 * on whether the feature is on.
 */
export function useVoiceConfigUpdate(handlers?: {
  onSaved?: () => void
  onError?: (message: string) => void
}) {
  const queryClient = useQueryClient()
  return useServerConfigUpdate({
    successMessage: "Voice transcription updated",
    errorMessage: voiceSaveErrorMessage,
    onError: handlers?.onError,
    onSaved: () => {
      void queryClient.invalidateQueries({ queryKey: VOICE_USAGE_QUERY_KEY })
      handlers?.onSaved?.()
    },
  })
}

/** Upper bound of the monthly limit: 31 days of minutes. */
export const MAX_MONTHLY_MINUTES = 44640

/**
 * "12 min", or "< 1 min" for a non-zero amount that rounds to nothing — so a
 * person who dictated for 20 seconds does not read as having used none.
 */
export function formatMinutes(seconds: number): string {
  const minutes = Math.round(seconds / 60)
  if (minutes === 0 && seconds > 0) return "< 1 min"
  return `${minutes} min`
}

/** "3 sessions" / "1 session". */
export function formatSessions(count: number): string {
  return `${count} ${count === 1 ? "session" : "sessions"}`
}

/** "No limit" / "60 minutes" — the limit row's value and nothing else. */
export function describeMonthlyLimit(minutes: number | null | undefined) {
  if (minutes == null) return "No limit"
  return `${minutes} ${minutes === 1 ? "minute" : "minutes"}`
}

/**
 * "November 2026" for the report's period. Read from the `YYYY-MM` prefix of
 * `period_start` rather than through `new Date(wire)` (naive UTC would be read
 * as local time), and from the server's period rather than the client clock.
 */
export function monthLabel(periodStart: string): string {
  const match = /^(\d{4})-(\d{2})/.exec(periodStart)
  if (!match) return "This month"
  return new Date(
    Date.UTC(Number(match[1]), Number(match[2]) - 1, 1),
  ).toLocaleString("en-US", { month: "long", year: "numeric", timeZone: "UTC" })
}
