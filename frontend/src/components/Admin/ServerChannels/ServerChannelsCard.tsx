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
import { ListRow, ListRowGroup, RowFlag } from "@/components/Common/ListRow"
import { OnOffToggle } from "@/components/Common/OnOffToggle"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
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
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import { ChannelDebugDialog } from "./ChannelDebugDialog"
import { ChannelSetupInstructionsPanel } from "./ChannelSetupInstructionsPanel"
import { parseWhitelist, WHITELIST_EMPTY_WARNING } from "./channelCopy"
import {
  DEFAULT_TRANSPORT_SHAPE,
  getChannelTypeMeta,
  resolvesExternalSenders,
  transportShapesByType,
} from "./channelTypes"
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

  // The declared transport shape per type. Shared query key with the dialog,
  // so opening "Add channel" costs nothing extra. A channel whose type is
  // missing here (still loading, or an adapter that left the registry) falls
  // back to the webhook shape — see `DEFAULT_TRANSPORT_SHAPE` for why that is
  // the safe direction to guess.
  const { data: channelTypes } = useQuery({
    queryKey: ["serverChannelTypes"],
    queryFn: () => ServerChannelsService.listChannelTypes(),
  })
  const shapes = transportShapesByType(channelTypes)

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
            <CardTitle className="flex items-center gap-2 min-w-0">
              <MessagesSquare className="h-5 w-5" />
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
            <ListRowGroup>
              {channels.map((channel) => {
                // `enabled` is optional in the generated type; derive once so
                // the dot, the row styling and the On/Off control cannot
                // disagree.
                const isEnabled = channel.enabled ?? true
                const shape =
                  shapes[channel.channel_type] ?? DEFAULT_TRANSPORT_SHAPE
                // Only meaningful for a transport that resolves an outside
                // sender. On an `authenticated` one the whitelist is never
                // consulted, so an empty one denies nobody — and the flag
                // would be a permanent false alarm right next to a Google Chat
                // row where it means the channel really is closed.
                const hasNoAllowedSenders =
                  resolvesExternalSenders(shape) &&
                  parseWhitelist(channel.email_whitelist ?? "").isEmpty
                // "No credential" means something different per transport —
                // a service account key for Google Chat, an SMTP server for
                // email — so the flag's explanation comes from the registry.
                // Absent ⇒ this transport has no outbound path at all, and a
                // flag with nothing to say in its tooltip is worse than none.
                const meta = getChannelTypeMeta(channel.channel_type)
                const missingCredentialsHint =
                  meta.outboundTest?.missingCredentialsHint
                // Scoped to this row: a pending toggle shouldn't freeze every
                // other channel's control.
                const isToggling =
                  toggleMutation.isPending &&
                  toggleMutation.variables?.id === channel.id
                return (
                  <ListRow
                    key={channel.id}
                    muted={!isEnabled}
                    status={{
                      tone: isEnabled ? "on" : "off",
                      label: isEnabled ? "Enabled" : "Disabled",
                    }}
                    title={channel.name}
                    // The transport is the row's one badge rather than a
                    // metadata line: it is a short word, it is what an admin
                    // scans this list by, and as a badge it costs no height —
                    // every row here stays a single line.
                    badges={
                      <Badge variant="outline" className="h-5 shrink-0 text-xs">
                        {channel.channel_type}
                      </Badge>
                    }
                    flags={
                      <>
                        {/* An empty whitelist denies everyone. The channel
                            looks healthy from here otherwise, so the
                            fail-closed state has to be visible in the list,
                            not only in the dialog. */}
                        {hasNoAllowedSenders && (
                          <RowFlag
                            icon={ShieldOff}
                            tone="warning"
                            label={WHITELIST_EMPTY_WARNING}
                          />
                        )}
                        {!channel.has_outbound_credentials &&
                          missingCredentialsHint && (
                            <RowFlag
                              icon={KeyRound}
                              tone="warning"
                              label={missingCredentialsHint}
                            />
                          )}
                      </>
                    }
                  >
                    {/* Being on or off is what an admin comes to this list to
                        change, so it is the row's one inline control — the
                        same Power glyph the `⋯` menus use elsewhere. */}
                    <OnOffToggle
                      checked={isEnabled}
                      disabled={isToggling}
                      label={channel.name}
                      onChange={(enabled) =>
                        toggleMutation.mutate({ id: channel.id, enabled })
                      }
                    />
                    <RowActionsMenu label={`the channel ${channel.name}`}>
                      <DropdownMenuItem
                        onSelect={() => setSetupChannel(channel)}
                      >
                        <Settings2 />
                        Setup instructions
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        onSelect={() => setDebugChannel(channel)}
                      >
                        <Bug />
                        Debug traffic
                      </DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => openEdit(channel)}>
                        <Pencil />
                        Edit channel
                      </DropdownMenuItem>
                      {/* A singleton channel has no delete: the backend
                          refuses it, because the row would be re-materialized
                          with default settings on the next read — turning
                          "delete" into a silent reset of a kill switch the
                          admin may have deliberately thrown. The On/Off
                          control is the operation they want. */}
                      {!shape.isSingleton && (
                        <>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem
                            variant="destructive"
                            onSelect={(e) => {
                              e.preventDefault()
                              setDeleting(channel)
                            }}
                          >
                            <Trash2 />
                            Delete channel
                          </DropdownMenuItem>
                        </>
                      )}
                    </RowActionsMenu>
                  </ListRow>
                )
              })}
            </ListRowGroup>
          )}
        </CardContent>
      </Card>

      <ServerChannelDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        channel={editing}
        existingChannelTypes={channels.map((c) => c.channel_type)}
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
          // The same declared shape the rows above branch on. The panel needs
          // it because "how is this channel reached" and "can it send" are
          // transport facts, and the stored columns it used to read
          // (`webhook_token`, `has_outbound_credentials`) answer neither for
          // an authenticated transport.
          transport={
            shapes[setupChannel.channel_type] ?? DEFAULT_TRANSPORT_SHAPE
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
              The channel stops accepting messages immediately and every
              conversation bound to it is discarded. People who message it
              afterwards will be routed as if for the first time.
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
