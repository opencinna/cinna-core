import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, Copy, RefreshCw, Send } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import {
  type ChannelTestOutboundRequest,
  type ChannelTestOutboundResult,
  type ServerChannelPublic,
  ServerChannelsService,
} from "@/client"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { LoadingButton } from "@/components/ui/loading-button"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"

/** Sentinel for the "type a raw id instead" option. A non-email string so it
 *  can never collide with a real sender address in the same Select. */
const CUSTOM_TARGET = "__custom__"

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  channel: ServerChannelPublic
}

/** Read-only value with a copy button. The webhook URL is long and must be
 *  pasted verbatim into a Google console field, so copying beats selecting. */
function CopyableValue({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const { showErrorToast } = useCustomToast()

  useEffect(
    () => () => {
      if (resetTimer.current) clearTimeout(resetTimer.current)
    },
    [],
  )

  const copy = async () => {
    // `navigator.clipboard` is undefined outside a secure context and
    // `writeText` rejects when permission is denied. Unhandled, both leave a
    // button that visibly does nothing.
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      resetTimer.current = setTimeout(() => setCopied(false), 2000)
    } catch {
      showErrorToast(`Failed to copy ${label.toLowerCase()}`)
    }
  }

  return (
    <div className="space-y-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <div className="flex items-center gap-2">
        <code className="flex-1 rounded border bg-background px-2 py-1.5 font-mono text-xs break-all">
          {value}
        </code>
        <Button
          variant="outline"
          size="icon"
          className="h-8 w-8 shrink-0"
          onClick={copy}
          aria-label={`Copy ${label}`}
        >
          {copied ? (
            <Check className="h-3 w-3 text-green-500" />
          ) : (
            <Copy className="h-3 w-3" />
          )}
        </Button>
      </div>
    </div>
  )
}

export function ChannelSetupInstructionsPanel({
  open,
  onOpenChange,
  channel,
}: Props) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [confirmRegenerate, setConfirmRegenerate] = useState(false)
  const [threadKey, setThreadKey] = useState("")
  // Which target the admin is aiming at. Email is the default because a raw
  // space id gives no clue where the message will land — the complaint this
  // picker exists to answer.
  const [target, setTarget] = useState<string>("")
  const [testResult, setTestResult] =
    useState<ChannelTestOutboundResult | null>(null)

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["serverChannelSetup", channel.id],
    queryFn: () =>
      ServerChannelsService.getSetupInstructions({ channelId: channel.id }),
    enabled: open,
  })

  // Everyone this channel has already seen. An email can only be resolved to a
  // destination the platform has observed — the provider's email alias needs
  // user authentication and this app authenticates as an app — so the picker
  // lists exactly the addresses that can actually work.
  const {
    data: senders = [],
    isLoading: sendersLoading,
    isError: sendersError,
  } = useQuery({
    queryKey: ["serverChannelRecentSenders", channel.id],
    queryFn: () =>
      ServerChannelsService.listRecentSenders({ channelId: channel.id }),
    enabled: open,
  })

  const testMutation = useMutation({
    mutationFn: (body: ChannelTestOutboundRequest) =>
      ServerChannelsService.testOutbound({
        channelId: channel.id,
        requestBody: body,
      }),
    // The route reports failure as a 200 with `success: false` — it is a
    // diagnostic, so the reason travels in the body rather than as an error.
    onSuccess: (result) => {
      setTestResult(result)
      if (result.success) showSuccessToast("Test message delivered")
      // The send is recorded as a `test_send` event and can surface a sender
      // the picker has not listed yet, so both feeds are now stale.
      queryClient.invalidateQueries({
        queryKey: ["serverChannelDebug", channel.id],
      })
      queryClient.invalidateQueries({
        queryKey: ["serverChannelRecentSenders", channel.id],
      })
    },
    onError: (err) =>
      setTestResult({
        success: false,
        error: getErrorMessage(err, "Test failed"),
      }),
  })

  const regenerateMutation = useMutation({
    mutationFn: () =>
      ServerChannelsService.updateChannel({
        channelId: channel.id,
        requestBody: { regenerate_webhook_token: true },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["serverChannels"] })
      queryClient.invalidateQueries({
        queryKey: ["serverChannelSetup", channel.id],
      })
      showSuccessToast("Webhook token regenerated — update the Google Chat app")
      setConfirmRegenerate(false)
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to regenerate token")),
  })

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-[620px] max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Set up {channel.name}</DialogTitle>
            <DialogDescription>
              Paste the webhook URL into the channel's app configuration, then
              send the bot a message to test it.
            </DialogDescription>
          </DialogHeader>

          {isError ? (
            <p className="text-sm text-destructive">
              {getErrorMessage(error, "Couldn't load setup instructions.")}
            </p>
          ) : isLoading || !data ? (
            <div className="space-y-2">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-24 w-full" />
            </div>
          ) : (
            <div className="space-y-4">
              <CopyableValue label="Webhook URL" value={data.webhook_url} />

              {Object.entries(data.details ?? {}).map(([label, value]) => (
                <div key={label} className="space-y-1">
                  <span className="text-xs text-muted-foreground">{label}</span>
                  <p className="font-mono text-xs break-all">{value}</p>
                </div>
              ))}

              {(data.steps ?? []).length > 0 && (
                <div className="space-y-2">
                  <span className="text-xs font-medium">Steps</span>
                  <ol className="list-decimal space-y-1.5 pl-5 text-sm text-muted-foreground">
                    {(data.steps ?? []).map((step, i) => (
                      <li key={i}>{step}</li>
                    ))}
                  </ol>
                </div>
              )}

              <div className="space-y-2 rounded-lg border p-3">
                <div className="space-y-0.5">
                  <p className="text-sm font-medium">Test outbound</p>
                  <p className="text-xs text-muted-foreground">
                    Posts a message to prove the stored credential works. Pick
                    someone who has messaged the app — the test lands in the
                    conversation they already have with it.
                  </p>
                </div>

                {/* A result names the address it was sent to, so it must not
                    outlive the target — it would read as a verdict about the
                    newly picked one. */}
                <Select
                  value={target}
                  onValueChange={(value) => {
                    setTarget(value)
                    setTestResult(null)
                  }}
                >
                  <SelectTrigger className="text-xs">
                    <SelectValue placeholder="Choose who to message…" />
                  </SelectTrigger>
                  <SelectContent>
                    {senders.map((sender) => (
                      <SelectItem key={sender.email} value={sender.email}>
                        {sender.display_name
                          ? `${sender.display_name} <${sender.email}>`
                          : sender.email}
                      </SelectItem>
                    ))}
                    <SelectItem value={CUSTOM_TARGET}>
                      Custom space or thread ID…
                    </SelectItem>
                  </SelectContent>
                </Select>

                {/* A failed fetch must never look like "nobody has messaged
                    this channel" — the admin would go re-check the chat app's
                    configuration when only this one admin GET failed. */}
                {target !== CUSTOM_TARGET &&
                  (sendersError ? (
                    <p className="text-xs text-destructive">
                      Couldn't load recent senders. You can still send to a
                      space or thread ID with "Custom space or thread ID…".
                    </p>
                  ) : sendersLoading ? (
                    <p className="text-xs text-muted-foreground">
                      Loading recent senders…
                    </p>
                  ) : senders.length === 0 ? (
                    <p className="text-xs text-muted-foreground">
                      Nobody has messaged this channel yet. An email can only be
                      resolved once the app has seen a message from that person,
                      so until then use a space or thread ID.
                    </p>
                  ) : null)}

                <div className="flex items-center gap-2">
                  {target === CUSTOM_TARGET && (
                    <Input
                      value={threadKey}
                      onChange={(e) => {
                        setThreadKey(e.target.value)
                        setTestResult(null)
                      }}
                      placeholder="spaces/AAAA"
                      className="font-mono text-xs"
                    />
                  )}
                  <LoadingButton
                    variant="outline"
                    size="sm"
                    className="shrink-0"
                    loading={testMutation.isPending}
                    disabled={
                      !channel.has_outbound_credentials ||
                      (target === CUSTOM_TARGET
                        ? !threadKey.trim()
                        : !target.trim())
                    }
                    onClick={() =>
                      testMutation.mutate(
                        target === CUSTOM_TARGET
                          ? { thread_key: threadKey.trim() }
                          : { email: target },
                      )
                    }
                  >
                    <Send className="mr-2 h-3.5 w-3.5" />
                    Send test
                  </LoadingButton>
                </div>
                {!channel.has_outbound_credentials && (
                  <p className="text-xs text-amber-600 dark:text-amber-400">
                    No credential stored yet — add the service account JSON to
                    this channel first.
                  </p>
                )}
                {/* The whole point of this control is the reason for failure,
                    so the error is rendered in place rather than only toasted. */}
                {testResult &&
                  (testResult.success ? (
                    <p className="text-xs text-green-600 dark:text-green-400">
                      Message delivered.
                    </p>
                  ) : (
                    <p className="text-xs text-destructive break-all">
                      {testResult.error || "Delivery failed."}
                    </p>
                  ))}
              </div>

              <div className="flex items-center justify-between gap-4 rounded-lg border border-destructive/40 p-3">
                <div className="min-w-0 space-y-0.5">
                  <p className="text-sm font-medium">
                    Regenerate webhook token
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Issues a new URL and immediately invalidates the current
                    one.
                  </p>
                </div>
                <Button
                  variant="destructive"
                  size="sm"
                  className="shrink-0"
                  onClick={() => setConfirmRegenerate(true)}
                >
                  <RefreshCw className="mr-2 h-3.5 w-3.5" />
                  Regenerate
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <AlertDialog open={confirmRegenerate} onOpenChange={setConfirmRegenerate}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Regenerate webhook token?</AlertDialogTitle>
            <AlertDialogDescription>
              The current webhook URL stops working immediately. Messages from{" "}
              {channel.name} will fail until you paste the new URL into the
              channel's app configuration.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={(e) => {
                e.preventDefault()
                regenerateMutation.mutate()
              }}
              disabled={regenerateMutation.isPending}
            >
              Regenerate
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
