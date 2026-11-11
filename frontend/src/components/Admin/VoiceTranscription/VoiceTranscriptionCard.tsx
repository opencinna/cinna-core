import { useMutation } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Activity, AudioLines, Pencil } from "lucide-react"
import { useState } from "react"
import { VoiceTranscriptionService } from "@/client"
import { ApiError } from "@/client/core/ApiError"
import {
  pendingConfigField,
  useServerConfig,
} from "@/components/Admin/AccessPolicy/accessPolicy"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { VoiceMonthlyLimitDialog } from "./VoiceMonthlyLimitDialog"
import {
  describeMonthlyLimit,
  useVoiceConfigUpdate,
  useVoiceProviders,
} from "./voiceTranscription"

/**
 * Whether Cinna Desktop users can dictate through this server, and which
 * company AI provider pays for it.
 *
 * Auto-save card: the master switch and the provider select persist on change;
 * the monthly limit is the one P2 summary row, edited in its own dialog. The
 * Test button probes the *saved* provider — with auto-save the selected one is
 * the saved one — and reports through a toast, never through text that stays.
 */
export function VoiceTranscriptionCard() {
  const [limitOpen, setLimitOpen] = useState(false)
  const config = useServerConfig()
  const providers = useVoiceProviders()
  const updateMutation = useVoiceConfigUpdate()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const enabled = config.data?.voice_transcription_enabled === true
  const providerId = config.data?.voice_transcription_provider_id || null
  const limitMinutes =
    config.data?.voice_transcription_monthly_minutes_per_user ?? null
  const options = providers.data ?? []
  const providerName = providerId
    ? options.find((option) => option.id === providerId)?.name || null
    : null
  const pendingField = pendingConfigField(updateMutation)

  const testMutation = useMutation({
    mutationFn: () =>
      VoiceTranscriptionService.testVoiceTranscriptionProvider({
        requestBody: {},
      }),
    onSuccess: (result) => {
      const name = providerName || "The provider"
      if (result.ok) {
        showSuccessToast(`${name} accepted a test session.`)
        return
      }
      switch (result.error) {
        case "invalid_key":
          showErrorToast(
            `${name} rejected its API key. Replace the key in AI providers.`,
          )
          return
        case "unreachable":
          showErrorToast(`Couldn't reach ${name}. Try again in a minute.`)
          return
        case "quota":
          showErrorToast(`${name}'s quota is used up.`)
          return
        case "disabled":
          showErrorToast(
            "The saved provider can't transcribe. Choose another provider.",
          )
          return
        default:
          showErrorToast("Couldn't run the test. Try again in a minute.")
      }
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 429) {
        showErrorToast("Too many tests — wait a minute.")
        return
      }
      showErrorToast("Couldn't run the test. Try again in a minute.")
    },
  })

  const description = () => {
    if (!config.data)
      return "Lets Cinna Desktop users dictate through this server."
    if (enabled && providerName) {
      return `Cinna Desktop users can dictate; audio is transcribed with ${providerName}.`
    }
    if (!enabled && !providerId) {
      return "Lets Cinna Desktop users dictate through this server. Choose a provider to turn it on."
    }
    return "Lets Cinna Desktop users dictate through this server."
  }

  const body = () => {
    // Gated on there being no data: a failed background refetch keeps the
    // last good value on screen rather than swapping the card for an error.
    if (
      (config.isError && config.data === undefined) ||
      (providers.isError && providers.data === undefined)
    ) {
      return (
        <QueryErrorAlert
          error={config.error ?? providers.error}
          fallback="Couldn't read the voice transcription settings."
          onRetry={() => {
            void config.refetch()
            void providers.refetch()
          }}
        />
      )
    }

    if (!config.data || !providers.data) {
      return (
        <div className="space-y-4">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-11 w-full" />
          <Skeleton className="h-8 w-28" />
        </div>
      )
    }

    return (
      <div className="space-y-4">
        {/* Data-driven, not toggle-driven: the provider was deleted under a
            switched-on feature, so the card's height changes only when the
            server's state does. */}
        {enabled && !providerId && (
          <Alert variant="destructive">
            <AlertDescription>
              Voice transcription is on but has no provider, so dictation is
              unavailable. Choose a provider.
            </AlertDescription>
          </Alert>
        )}

        <div className="flex flex-col gap-2 xl:flex-row xl:items-center xl:justify-between xl:gap-4">
          <div className="min-w-0">
            <Label
              htmlFor="voice-transcription-provider"
              className="text-sm font-medium"
            >
              Provider
            </Label>
            <p className="text-xs text-muted-foreground">
              Company AI provider that pays for transcription. Only Google
              API-key providers can transcribe.
            </p>
          </div>
          {options.length === 0 ? (
            // Same row, no control to offer: the one thing to do is add a
            // provider, and that happens on another page.
            <p className="text-sm text-muted-foreground xl:w-[240px] xl:shrink-0">
              No provider can transcribe yet. Add a Google provider in{" "}
              <Link
                to="/admin/ai-credentials"
                hash="providers"
                className="text-primary underline-offset-4 hover:underline"
              >
                AI providers
              </Link>
              .
            </p>
          ) : (
            <Select
              // A saved provider no longer on the list (deleted, or no longer
              // speech-capable) shows the placeholder, not an empty trigger.
              value={providerName ? (providerId ?? undefined) : undefined}
              onValueChange={(value) =>
                updateMutation.mutate({
                  voice_transcription_provider_id: value,
                })
              }
              disabled={pendingField === "voice_transcription_provider_id"}
            >
              <SelectTrigger
                id="voice-transcription-provider"
                className="w-full xl:w-[240px] xl:shrink-0"
              >
                <SelectValue placeholder="Choose a provider" />
              </SelectTrigger>
              <SelectContent>
                {options.map((option) => (
                  <SelectItem key={option.id} value={option.id}>
                    {option.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </div>

        <div className="flex items-start justify-between gap-3 rounded-md border px-3 py-2.5">
          <div className="min-w-0">
            <p className="text-xs text-muted-foreground">
              Monthly limit per person
            </p>
            <p className="text-sm font-medium">
              {describeMonthlyLimit(limitMinutes)}
            </p>
          </div>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                onClick={() => setLimitOpen(true)}
                aria-label="Edit monthly limit"
              >
                <Pencil className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Edit monthly limit</TooltipContent>
          </Tooltip>
        </div>

        <div>
          <LoadingButton
            variant="outline"
            size="sm"
            onClick={() => testMutation.mutate()}
            loading={testMutation.isPending}
            disabled={!providerId}
          >
            {!testMutation.isPending && <Activity className="h-4 w-4" />}
            {testMutation.isPending ? "Testing…" : "Test provider"}
          </LoadingButton>
        </div>
      </div>
    )
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <AudioLines className="h-5 w-5 shrink-0" />
          <span className="truncate">Voice transcription</span>
          {/* Off and with no saved provider there is nothing to switch on;
              the description says why. Kept live while on, so a feature whose
              provider was deleted can still be turned off. */}
          <Switch
            className="-my-0.5 ml-auto"
            checked={enabled}
            onCheckedChange={(checked) =>
              updateMutation.mutate({ voice_transcription_enabled: checked })
            }
            disabled={
              !config.data ||
              (!enabled && !providerId) ||
              pendingField === "voice_transcription_enabled"
            }
            aria-label="Voice transcription"
          />
        </CardTitle>
        <CardDescription>
          {/* Derived from the persisted row only. */}
          {description()}
        </CardDescription>
      </CardHeader>
      <CardContent>{body()}</CardContent>
      <VoiceMonthlyLimitDialog
        open={limitOpen}
        onOpenChange={setLimitOpen}
        persisted={limitMinutes}
      />
    </Card>
  )
}
