import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Bug,
  KeyRound,
  MessagesSquare,
  Pencil,
  Plus,
  Settings2,
  ShieldOff,
  Trash2,
} from "lucide-react"
import { useState } from "react"

import { type ServerChannelPublic, ServerChannelsService } from "@/client"
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
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { ChannelDebugDialog } from "./ChannelDebugDialog"
import { ChannelSetupInstructionsPanel } from "./ChannelSetupInstructionsPanel"
import { parseWhitelist, WHITELIST_EMPTY_WARNING } from "./channelCopy"
import { ServerChannelDialog } from "./ServerChannelDialog"

export function ServerChannelsCard() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<ServerChannelPublic | null>(null)
  const [debugChannel, setDebugChannel] = useState<ServerChannelPublic | null>(
    null,
  )
  const [setupChannel, setSetupChannel] = useState<ServerChannelPublic | null>(
    null,
  )
  const [deleting, setDeleting] = useState<ServerChannelPublic | null>(null)

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["serverChannels"],
    queryFn: () => ServerChannelsService.listChannels(),
  })

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      ServerChannelsService.updateChannel({
        channelId: id,
        requestBody: { enabled },
      }),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ["serverChannels"] })
      showSuccessToast(
        variables.enabled ? "Channel enabled" : "Channel disabled",
      )
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to update channel")),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) =>
      ServerChannelsService.deleteChannel({ channelId: id }),
    onSuccess: (_data, deletedId) => {
      queryClient.invalidateQueries({ queryKey: ["serverChannels"] })
      // Drop the dead channel's setup cache and close its panel if it is open —
      // otherwise the fallback below keeps rendering a deleted channel's
      // webhook URL as though it were still live.
      queryClient.removeQueries({ queryKey: ["serverChannelSetup", deletedId] })
      setSetupChannel((current) => (current?.id === deletedId ? null : current))
      setDebugChannel((current) => (current?.id === deletedId ? null : current))
      queryClient.removeQueries({
        queryKey: ["serverChannelDebug", deletedId],
      })
      queryClient.removeQueries({
        queryKey: ["serverChannelRecentSenders", deletedId],
      })
      showSuccessToast("Channel deleted")
      setDeleting(null)
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to delete channel")),
  })

  const channels = data ?? []

  const openCreate = () => {
    setEditing(null)
    setDialogOpen(true)
  }

  const openEdit = (channel: ServerChannelPublic) => {
    setEditing(channel)
    setDialogOpen(true)
  }

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="flex items-center gap-2">
              <MessagesSquare className="h-4 w-4 text-blue-500" />
              Channels
            </CardTitle>
            <Button onClick={openCreate} size="sm">
              <Plus className="mr-2 h-4 w-4" />
              Add Channel
            </Button>
          </div>
          <CardDescription>
            Let people outside the platform talk to agents from a chat app
          </CardDescription>
        </CardHeader>
        <CardContent>
          {/* A failed fetch must never look like "nothing configured" — an
              admin would add a duplicate channel on the strength of it. */}
          {isError ? (
            <p className="text-sm text-destructive">
              {getErrorMessage(error, "Couldn't load channels.")}
            </p>
          ) : isLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-11 w-full" />
              <Skeleton className="h-11 w-full" />
            </div>
          ) : channels.length === 0 ? (
            <div className="py-6 text-center text-sm text-muted-foreground">
              <MessagesSquare className="mx-auto mb-2 h-8 w-8 opacity-50" />
              <p>No channels configured</p>
              <p className="mt-1 text-xs">
                Connect a chat app to let your team talk to agents.
              </p>
            </div>
          ) : (
            <div className="space-y-1.5">
              {channels.map((channel) => {
                // `enabled` is optional in the generated type; derive once so
                // the dot, the row styling and the switch cannot disagree.
                const isEnabled = channel.enabled ?? true
                const hasNoAllowedSenders = parseWhitelist(
                  channel.email_whitelist ?? "",
                ).isEmpty
                return (
                  <div
                    key={channel.id}
                    className={`flex items-center justify-between rounded-lg border px-3 py-2 ${
                      isEnabled ? "" : "bg-muted opacity-60"
                    }`}
                  >
                    <div className="flex min-w-0 items-center gap-2">
                      <span
                        className={`h-2 w-2 shrink-0 rounded-full ${
                          isEnabled ? "bg-green-500" : "bg-muted-foreground"
                        }`}
                        aria-hidden
                      />
                      <span className="truncate text-sm font-medium">
                        {channel.name}
                      </span>
                      <Badge variant="outline" className="shrink-0 text-xs">
                        {channel.channel_type}
                      </Badge>
                      {/* An empty whitelist denies everyone. The channel looks
                        healthy from here otherwise, so the fail-closed state
                        has to be visible in the list, not only in the dialog. */}
                      {hasNoAllowedSenders && (
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Badge
                                variant="outline"
                                className="shrink-0 gap-1 border-amber-500/50 text-xs text-amber-600 dark:text-amber-400"
                              >
                                <ShieldOff className="h-3 w-3" />
                                No allowed senders
                              </Badge>
                            </TooltipTrigger>
                            <TooltipContent className="max-w-xs text-xs">
                              {WHITELIST_EMPTY_WARNING}
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                      )}
                      {!channel.has_outbound_credentials && (
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Badge
                                variant="outline"
                                className="shrink-0 gap-1 border-amber-500/50 text-xs text-amber-600 dark:text-amber-400"
                              >
                                <KeyRound className="h-3 w-3" />
                                No credential
                              </Badge>
                            </TooltipTrigger>
                            <TooltipContent className="max-w-xs text-xs">
                              No service account key stored, so the agent's
                              replies can't be delivered. Edit the channel to
                              add one.
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                      )}
                    </div>

                    <div className="ml-2 flex shrink-0 items-center gap-1">
                      <TooltipProvider>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span>
                              <Switch
                                checked={isEnabled}
                                // Scoped to this row: a pending toggle shouldn't
                                // freeze every other channel's switch.
                                disabled={
                                  toggleMutation.isPending &&
                                  toggleMutation.variables?.id === channel.id
                                }
                                aria-label={
                                  isEnabled
                                    ? `Disable ${channel.name}`
                                    : `Enable ${channel.name}`
                                }
                                onCheckedChange={(checked) =>
                                  toggleMutation.mutate({
                                    id: channel.id,
                                    enabled: checked,
                                  })
                                }
                              />
                            </span>
                          </TooltipTrigger>
                          <TooltipContent>
                            {isEnabled ? "Disable" : "Enable"}
                          </TooltipContent>
                        </Tooltip>
                      </TooltipProvider>

                      <div className="mx-1 h-4 w-px bg-border" />

                      <TooltipProvider>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7"
                              onClick={() => setSetupChannel(channel)}
                            >
                              <Settings2 className="h-3.5 w-3.5" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>Setup instructions</TooltipContent>
                        </Tooltip>
                      </TooltipProvider>

                      <TooltipProvider>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7"
                              onClick={() => setDebugChannel(channel)}
                              aria-label="Debug channel"
                            >
                              <Bug className="h-3.5 w-3.5" />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent>
                            Debug — live inbound / outbound traffic
                          </TooltipContent>
                        </Tooltip>
                      </TooltipProvider>

                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7"
                        onClick={() => openEdit(channel)}
                        aria-label="Edit channel"
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 text-destructive hover:text-destructive"
                        onClick={() => setDeleting(channel)}
                        aria-label="Delete channel"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </CardContent>
      </Card>

      <ServerChannelDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        channel={editing}
        onCreated={(created) => setSetupChannel(created)}
      />

      {setupChannel && (
        <ChannelSetupInstructionsPanel
          open={setupChannel !== null}
          onOpenChange={(open) => !open && setSetupChannel(null)}
          channel={
            // Re-read from the list so the panel follows a token regeneration
            // instead of showing the value captured when it was opened.
            channels.find((c) => c.id === setupChannel.id) ?? setupChannel
          }
        />
      )}

      {debugChannel && (
        <ChannelDebugDialog
          open={debugChannel !== null}
          onOpenChange={(open) => !open && setDebugChannel(null)}
          channel={
            channels.find((c) => c.id === debugChannel.id) ?? debugChannel
          }
        />
      )}

      <AlertDialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {deleting?.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              The webhook stops working immediately and every conversation bound
              to it is discarded. People who message the bot afterwards will be
              routed as if for the first time.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={(e) => {
                e.preventDefault()
                if (deleting) deleteMutation.mutate(deleting.id)
              }}
              disabled={deleteMutation.isPending}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
