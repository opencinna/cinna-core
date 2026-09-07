/**
 * One channel, as a compact P3 row.
 *
 * Rendered by both the Channels card and its "Show all" Sheet so the two hosts
 * cannot drift. The row carries the one control whose purpose it is — the
 * on/off `Switch` — and nothing else inline; everything structured lives one
 * level down, in the Configure Sheet the `⋯` menu opens.
 *
 * WHY INHERITANCE IS LABELLED RATHER THAN JUST RENDERED
 * -----------------------------------------------------
 * A switch showing "on" because an admin default says so looks identical to
 * one the user turned on themselves, and the two behave differently the moment
 * the admin changes their mind: the first follows, the second does not. So the
 * metadata line says which of the two this is, its tooltip names the default
 * being followed, and every explicitly-set field offers a way back to
 * following it.
 *
 * `is_available` AND `is_enabled` ARE DIFFERENT FACTS
 * ---------------------------------------------------
 * `is_enabled` is the user's own switch. `is_available` is the whole
 * conjunction — the admin kill switch, access, *and* that switch. They are
 * rendered separately, and a channel that is switched on by its user but not
 * available says so instead of silently showing "on".
 *
 * The update/reset mutations and the Discard confirm are owned by the row
 * rather than by its host: that keeps the "Show all" Sheet open behind the
 * confirm, and keeps the card and the Sheet on one code path. The update goes
 * through `useChannelUpdate`, which is also what the Configure Sheet writes
 * through — one queue per channel, so a switch flipped here cannot race a
 * checklist tick made there.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  EllipsisVertical,
  Loader2,
  RotateCcw,
  Settings2,
  Undo2,
} from "lucide-react"
import { useState } from "react"

import type { UserChannelPublic } from "@/client"
import { UserChannelsService } from "@/client"
import { getChannelTypeMeta } from "@/components/Admin/ServerChannels/channelTypes"
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Switch } from "@/components/ui/switch"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import { getErrorMessage } from "@/utils"
import {
  inheritCaption,
  isBlockedByAdmin,
  scopeSummary,
  UNAVAILABLE_SENTENCE,
  USER_CHANNELS_KEY,
  writeChannelToCache,
} from "./channelScopes"
import { useChannelUpdate } from "./useChannelUpdate"

interface ChannelRowProps {
  channel: UserChannelPublic
  /** Opens the Configure Sheet. The host decides what else happens first. */
  onConfigure: (channel: UserChannelPublic) => void
}

export function ChannelRow({ channel, onConfigure }: ChannelRowProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [confirmOpen, setConfirmOpen] = useState(false)

  const meta = getChannelTypeMeta(channel.channel_type)
  const Icon = meta.icon

  const blockedByAdmin = isBlockedByAdmin(channel)

  const updateMutation = useChannelUpdate(channel.id)

  const resetMutation = useMutation({
    // Same scope as the update: a DELETE that overtakes a queued PUT would be
    // undone by it.
    scope: { id: `channel-${channel.id}` },
    mutationFn: () =>
      UserChannelsService.resetMyChannel({ channelId: channel.id }),
    onMutate: () => queryClient.cancelQueries({ queryKey: USER_CHANNELS_KEY }),
    onSuccess: (updated) => {
      writeChannelToCache(queryClient, updated)
      showSuccessToast("Back to the administrator's defaults")
      setConfirmOpen(false)
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to reset channel settings")),
  })

  // Pending is scoped to this row: only the row being written to loses its
  // controls.
  const isBusy = updateMutation.isPending || resetMutation.isPending

  // Two words, not a clause: the metadata line has ~190px at 1024 in a
  // half-width card and must fit BOTH facts. The full sentence is one hover
  // away, on this same text.
  const provenance = channel.is_enabled_inherited
    ? "Admin default"
    : "Set by you"

  const defaultSentence = inheritCaption(
    channel.is_enabled_inherited,
    channel.channel_default_enabled ? "on" : "off",
  )

  return (
    <div
      className={cn(
        "flex items-center justify-between px-3 py-2 border rounded-lg",
        !channel.is_enabled && "opacity-60",
      )}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <div className="w-6 h-6 rounded-md shrink-0 flex items-center justify-center bg-muted">
            <Icon className={cn("h-3.5 w-3.5", meta.iconClass)} />
          </div>
          <span className="text-sm font-medium truncate min-w-0">
            {channel.name}
          </span>
          {/* No "Default" / "Your choice" badge: it would repeat, in ~60px of
              a name's ~90px at 1024, exactly what the metadata line's first
              fact already says. Only the two facts that line CANNOT carry are
              badged — the channel being unavailable, and identity routing
              being on. */}
          {blockedByAdmin && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="destructive" className="text-xs shrink-0">
                  Unavailable
                </Badge>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-xs text-xs">
                {UNAVAILABLE_SENTENCE}
              </TooltipContent>
            </Tooltip>
          )}
          {/* Only when on. Identity routing is off by default, so a badge on
              every row would be noise about a feature most people are not
              using — while the people who ARE need to see it at a glance. */}
          {channel.allow_identity_routing && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="secondary" className="text-xs shrink-0">
                  + identities
                </Badge>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-xs text-xs">
                This channel may also reach an agent someone has shared with
                you, in a session that lives in their workspace.
              </TooltipContent>
            </Tooltip>
          )}
        </div>
        <p className="text-xs text-muted-foreground truncate mt-0.5">
          {/* Provenance carries its own sentence, which is where the badge's
              tooltip used to live: which default is being followed, and what
              happens if the administrator changes it. */}
          <Tooltip>
            <TooltipTrigger asChild>
              {/* A `button` only so the sentence is reachable by keyboard —
                  this tooltip is its only home, and a `span` cannot take
                  focus (`tabIndex` on one is a lint error, correctly). It
                  performs nothing, so the row's inline action budget is still
                  the Switch and the menu. */}
              <button
                type="button"
                className="cursor-default underline decoration-dotted underline-offset-2"
              >
                {provenance}
              </button>
            </TooltipTrigger>
            <TooltipContent side="top" className="max-w-xs text-xs">
              {defaultSentence}
            </TooltipContent>
          </Tooltip>{" "}
          · {scopeSummary(channel)}
        </p>
      </div>

      <div className="flex items-center gap-0.5 shrink-0">
        {/* A reserved slot, always present: a spinner that appears on write
            must not push the switch sideways. It is a pending indicator, not
            an action — the row's inline budget is the Switch and the menu. */}
        <span className="flex h-3.5 w-3.5 items-center justify-center">
          {isBusy && (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
          )}
        </span>
        <Switch
          checked={channel.is_enabled}
          disabled={isBusy}
          aria-label={`Enable ${channel.name} for me`}
          // Inherited values are dimmed so the row reads as "this is not (yet)
          // yours" at a glance, matching the Default badge.
          className={cn(channel.is_enabled_inherited && "opacity-70")}
          onCheckedChange={(next) =>
            updateMutation.mutate({ is_enabled: next })
          }
        />
        <DropdownMenu>
          <Tooltip>
            <TooltipTrigger asChild>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  disabled={isBusy}
                  aria-label={`Actions for ${channel.name}`}
                >
                  <EllipsisVertical className="h-3.5 w-3.5" />
                </Button>
              </DropdownMenuTrigger>
            </TooltipTrigger>
            <TooltipContent side="top" className="text-xs">
              More actions
            </TooltipContent>
          </Tooltip>
          <DropdownMenuContent align="end">
            <DropdownMenuItem
              onSelect={(e) => {
                e.preventDefault()
                onConfigure(channel)
              }}
            >
              <Settings2 />
              Configure
            </DropdownMenuItem>
            {!channel.is_enabled_inherited && (
              <DropdownMenuItem
                onSelect={() =>
                  // An explicit `null` clears just this field, returning it to
                  // the channel default while keeping the agent choices. That
                  // is what the API's "explicit null = inherit" contract is
                  // for.
                  updateMutation.mutate({ is_enabled: null })
                }
              >
                <RotateCcw />
                Follow the default again
              </DropdownMenuItem>
            )}
            {/* The only way back to *pure* inheritance once anything has been
                written — the per-field paths above clear one field each, but a
                row can exist with every field already cleared. Offered only
                when a row actually exists, because DELETE is a no-op
                otherwise. */}
            {channel.has_settings && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  variant="destructive"
                  onSelect={(e) => {
                    e.preventDefault()
                    setConfirmOpen(true)
                  }}
                >
                  <Undo2 />
                  Discard all my settings
                </DropdownMenuItem>
              </>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <AlertDialog
        open={confirmOpen}
        // Escape would otherwise close the confirm mid-request, leaving the
        // pending state with nowhere to live.
        onOpenChange={(next) => {
          if (!resetMutation.isPending) setConfirmOpen(next)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Follow the administrator's defaults again?
            </AlertDialogTitle>
            <AlertDialogDescription>
              This discards everything you have set for{" "}
              <span className="font-medium text-foreground">
                {channel.name}
              </span>{" "}
              — your on/off switch, your agent choices, and your identity
              routing opt-in, which goes back to off. From then on the channel
              follows the administrator's defaults, including any future change
              to them. The people you have enabled are kept: those are
              person-level and are not this channel's to discard.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={resetMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                // Keep the confirm open while the request is in flight so the
                // pending state has somewhere to live.
                e.preventDefault()
                resetMutation.mutate()
              }}
              disabled={resetMutation.isPending}
            >
              {resetMutation.isPending && (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              )}
              Follow defaults
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
