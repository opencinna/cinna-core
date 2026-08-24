/**
 * UserChannelsCard — Settings → Channels.
 *
 * One row per channel an administrator has made available to this user, with
 * the two things the user actually controls: whether the channel is on for
 * them, and which of their agents it may reach.
 *
 * THE ONE RULE THIS COMPONENT IS ORGANISED AROUND
 * -----------------------------------------------
 * **The inherit rules are not implemented here, and must never be.** Every
 * value on `UserChannelPublic` arrives already resolved by the backend's
 * `ChannelPolicyService`, together with an `*_inherited` flag and the
 * `channel_default_*` value it followed. This file reads those; it never
 * recomputes "is this channel on for me" from a default and an override. A
 * second implementation of the rules is precisely how a settings page comes to
 * say a channel is on while the router treats it as off — a disagreement that
 * is undiagnosable from either side.
 *
 * WHY INHERITANCE IS LABELLED RATHER THAN JUST RENDERED
 * -----------------------------------------------------
 * A switch showing "on" because an admin default says so looks identical to
 * one the user turned on themselves, and the two behave differently the moment
 * the admin changes their mind: the first follows, the second does not. So an
 * inherited value is badged as inherited, its caption names the default it is
 * following, and every explicitly-set field offers a way back to following it.
 *
 * `is_available` AND `is_enabled` ARE DIFFERENT FACTS
 * ---------------------------------------------------
 * `is_enabled` is the user's own switch. `is_available` is the whole
 * conjunction — the admin kill switch, access, *and* that switch. They are
 * rendered separately, and a channel that is switched on by its user but not
 * available says so instead of silently showing "on".
 *
 * The identity toggle (`allow_identity_routing`) is deliberately NOT surfaced:
 * it is Phase 3's control. The field exists on the wire model; nothing here
 * sends it, so it is left untouched by every write this component makes.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Loader2,
  RotateCcw,
} from "lucide-react"
import { useState } from "react"
import {
  AgentsService,
  type UserChannelPublic,
  UserChannelsService,
  type UserChannelUpdate,
} from "@/client"
import { getChannelTypeMeta } from "@/components/Admin/ServerChannels/channelTypes"
import { Alert, AlertDescription } from "@/components/ui/alert"
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
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"

const SCOPE_ALL = "all"
const SCOPE_LIST = "list"
const SCOPE_NONE = "none"

/**
 * User-facing wording for the three scopes.
 *
 * Deliberately not shared with the admin form's copy: the admin is choosing a
 * default for other people ("all their agents"), the user is choosing for
 * themselves ("all my agents"). Same values, different sentence.
 */
const SCOPE_OPTIONS: ReadonlyArray<{
  value: string
  label: string
  hint: string
}> = [
  {
    value: SCOPE_ALL,
    label: "All my agents",
    hint: "Any agent I own can answer on this channel.",
  },
  {
    value: SCOPE_LIST,
    label: "Only the agents I pick",
    hint: "Just the agents ticked below.",
  },
  {
    value: SCOPE_NONE,
    label: "None",
    hint: "Nothing routes to me on this channel.",
  },
]

/**
 * The subset of `UserChannelUpdate` this phase is allowed to write.
 *
 * Derived from the generated type rather than hand-declared, so a change to
 * the wire model reaches these call sites — but narrowed with `Pick`, which is
 * what structurally prevents Phase 2 from writing `allow_identity_routing`
 * (Phase 3's control) or `pinned_agent_id` (no UI writes it yet).
 *
 * `null` stays in the value type on purpose: on the two inheritable fields it
 * is not "no value", it is "revert this field to the channel default".
 */
type ChannelPatch = Pick<
  UserChannelUpdate,
  "is_enabled" | "agent_scope" | "agent_ids"
>

function scopeLabel(scope: string): string {
  // Falls through to the raw value rather than a blank: the scope vocabulary
  // is a plain string column that can grow without a client release.
  return SCOPE_OPTIONS.find((o) => o.value === scope)?.label ?? scope
}

export function UserChannelsCard() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [resetting, setResetting] = useState<UserChannelPublic | null>(null)

  const {
    data: channels,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ["userChannels"],
    queryFn: () => UserChannelsService.listMyChannels(),
  })

  // The agent list is only needed to render (and tick) the per-channel
  // selection, so it is not fetched until a row is actually open. Owner-scoped
  // server-side — `GET /agents/` returns only the caller's own agents, which is
  // the same set the backend will accept in `agent_ids`.
  const AGENT_PAGE_SIZE = 200
  const {
    data: agentsData,
    isError: isAgentsError,
    isLoading: isAgentsLoading,
  } = useQuery({
    queryKey: ["allAgents"],
    queryFn: () =>
      AgentsService.readAgents({ skip: 0, limit: AGENT_PAGE_SIZE }),
    enabled: expanded.size > 0,
  })
  const agents = agentsData?.data ?? []
  // `count` here IS a real total (`GET /agents/` returns one), unlike the
  // `count` on `GET /users/search`, which is a page size. Used only to admit
  // that the checklist is truncated — never rendered as the number of agents
  // shown.
  const agentsTruncated = (agentsData?.count ?? 0) > agents.length

  /**
   * Write the resolved row the server just returned straight into the cache.
   *
   * Both PUT and DELETE answer with the re-resolved `UserChannelPublic`
   * precisely so the client does not have to guess — `user_channels.py` says
   * so in as many words. Invalidating instead would settle the mutation before
   * the refetch lands, leaving a window in which `isBusy` is false and the row
   * still renders pre-write state: the switch snaps back, and worse, a second
   * tick in the agent checklist would build its replace-the-whole-list payload
   * from the stale selection and drop the first one.
   */
  const writeChannelToCache = (updated: UserChannelPublic) =>
    queryClient.setQueryData<UserChannelPublic[]>(["userChannels"], (old) =>
      old?.map((c) => (c.id === updated.id ? updated : c)),
    )

  const updateMutation = useMutation({
    mutationFn: ({
      channelId,
      body,
    }: {
      channelId: string
      body: ChannelPatch
    }) => UserChannelsService.updateMyChannel({ channelId, requestBody: body }),
    onSuccess: writeChannelToCache,
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to save channel settings")),
  })

  const resetMutation = useMutation({
    mutationFn: (channelId: string) =>
      UserChannelsService.resetMyChannel({ channelId }),
    onSuccess: (updated) => {
      writeChannelToCache(updated)
      showSuccessToast("Back to the administrator's defaults")
      setResetting(null)
    },
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to reset channel settings")),
  })

  const toggleExpanded = (id: string) =>
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const isBusy = (channelId: string) =>
    (updateMutation.isPending &&
      updateMutation.variables?.channelId === channelId) ||
    (resetMutation.isPending && resetMutation.variables === channelId)

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <CardTitle>Channels</CardTitle>
          <CardDescription>
            Chat apps an administrator has connected to this platform. Choose
            whether each one can reach you, and which of your agents it may use.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-14 w-full" />
              <Skeleton className="h-14 w-full" />
            </div>
          ) : isError ? (
            /* A failed fetch must never read as "no channels are configured" —
               a user would conclude nothing can reach them, which is the
               opposite of what an unknown state warrants. */
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertDescription>
                Couldn't load your channels. This is a failed request, not an
                empty list — {getErrorMessage(error, "please try again")}.
              </AlertDescription>
            </Alert>
          ) : (channels ?? []).length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No channels are available to you. An administrator connects them
              in Server Configuration. If one you were using has disappeared
              from this list, it has been disabled or your access to it was
              removed — anything you had set for it is kept and applies again if
              that changes.
            </p>
          ) : (
            <div className="space-y-2">
              {(channels ?? []).map((channel) => (
                <ChannelRow
                  key={channel.id}
                  channel={channel}
                  agents={agents}
                  agentsFailed={isAgentsError}
                  agentsLoading={isAgentsLoading}
                  agentsTruncated={agentsTruncated}
                  isOpen={expanded.has(channel.id)}
                  isBusy={isBusy(channel.id)}
                  onToggleOpen={() => toggleExpanded(channel.id)}
                  onPatch={(body) =>
                    updateMutation.mutate({ channelId: channel.id, body })
                  }
                  onRequestReset={() => setResetting(channel)}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <AlertDialog
        open={resetting !== null}
        onOpenChange={(open) => !open && setResetting(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Follow the administrator's defaults again?
            </AlertDialogTitle>
            <AlertDialogDescription>
              This discards everything you have set for{" "}
              <span className="font-medium text-foreground">
                {resetting?.name}
              </span>{" "}
              — your on/off switch and your agent choices. From then on the
              channel follows the administrator's defaults, including any future
              change to them.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                if (resetting) resetMutation.mutate(resetting.id)
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
    </>
  )
}

interface AgentOption {
  id: string
  name: string
}

interface ChannelRowProps {
  channel: UserChannelPublic
  agents: AgentOption[]
  agentsFailed: boolean
  agentsLoading: boolean
  agentsTruncated: boolean
  isOpen: boolean
  isBusy: boolean
  onToggleOpen: () => void
  onPatch: (body: ChannelPatch) => void
  onRequestReset: () => void
}

function ChannelRow({
  channel,
  agents,
  agentsFailed,
  agentsLoading,
  agentsTruncated,
  isOpen,
  isBusy,
  onToggleOpen,
  onPatch,
  onRequestReset,
}: ChannelRowProps) {
  const meta = getChannelTypeMeta(channel.channel_type)
  const Icon = meta.icon
  const selectedAgentIds = channel.agent_ids ?? []
  const scope = channel.agent_scope

  // The user's switch says on, but the channel still isn't usable. The only
  // remaining terms of the conjunction are the admin kill switch and access,
  // and neither is the user's doing — so this is reported, not folded into the
  // switch. Conflating them would tell someone their channel is off when what
  // actually happened is that it was taken away from them.
  const blockedByAdmin = channel.is_enabled && !channel.is_available

  return (
    <div className="rounded-lg border">
      <div className="flex items-center gap-3 px-3 py-2.5">
        <Icon className={`h-4 w-4 shrink-0 ${meta.iconClass}`} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium">{channel.name}</span>
            {channel.is_enabled_inherited ? (
              <Badge variant="outline" className="shrink-0 text-[10px]">
                Default
              </Badge>
            ) : (
              <Badge variant="secondary" className="shrink-0 text-[10px]">
                Your choice
              </Badge>
            )}
          </div>
          {/* The honest caption. An inherited value names the default it is
              following and says it will keep following it; an explicit one
              says the default no longer applies. */}
          <p className="mt-0.5 text-xs text-muted-foreground">
            {channel.is_enabled_inherited
              ? `Following the administrator's default (${
                  channel.channel_default_enabled ? "on" : "off"
                }) — it changes if they change it.`
              : `You set this. The administrator's default is ${
                  channel.channel_default_enabled ? "on" : "off"
                }.`}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {isBusy && (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
          )}
          <Switch
            checked={channel.is_enabled}
            disabled={isBusy}
            aria-label={`Enable ${channel.name} for me`}
            // Inherited values are dimmed so the strip reads as "this is not
            // (yet) yours" at a glance, matching the Default badge.
            className={channel.is_enabled_inherited ? "opacity-70" : undefined}
            onCheckedChange={(next) => onPatch({ is_enabled: next })}
          />
        </div>
      </div>

      {!channel.is_enabled_inherited && (
        <div className="px-3 pb-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-6 px-1.5 text-xs text-muted-foreground"
            disabled={isBusy}
            // An explicit `null` clears just this field, returning it to the
            // channel default while keeping the agent choices. That is what the
            // API's "explicit null = inherit" contract is for.
            onClick={() => onPatch({ is_enabled: null })}
          >
            <RotateCcw className="mr-1 h-3 w-3" />
            Follow the default again
          </Button>
        </div>
      )}

      {blockedByAdmin && (
        <div className="px-3 pb-2">
          <Alert>
            <AlertTriangle className="h-4 w-4" />
            <AlertDescription>
              You have this channel switched on, but it isn't available to you
              right now — an administrator has either disabled it or removed
              your access. Your setting is kept and takes effect again if that
              changes.
            </AlertDescription>
          </Alert>
        </div>
      )}

      <button
        type="button"
        onClick={onToggleOpen}
        className="flex w-full items-center gap-1.5 border-t px-3 py-2 text-xs text-muted-foreground transition-colors hover:text-foreground"
        aria-expanded={isOpen}
        aria-controls={`${channel.id}-agent-panel`}
      >
        {isOpen ? (
          <ChevronDown className="h-3.5 w-3.5" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5" />
        )}
        Agents this channel can reach
        <span className="ml-auto flex items-center gap-1.5">
          {channel.agent_scope_inherited && (
            <Badge variant="outline" className="text-[10px]">
              Default
            </Badge>
          )}
          <span>
            {scope === SCOPE_LIST
              ? `${selectedAgentIds.length} picked`
              : scopeLabel(scope)}
          </span>
        </span>
      </button>

      {isOpen && (
        <div
          id={`${channel.id}-agent-panel`}
          className="space-y-3 border-t px-3 py-3"
        >
          <RadioGroup
            value={scope}
            disabled={isBusy}
            onValueChange={(next) => onPatch({ agent_scope: next })}
            className="gap-2"
          >
            {SCOPE_OPTIONS.map((option) => {
              const isDefault =
                option.value === channel.channel_default_agent_scope
              return (
                <div key={option.value} className="flex items-start gap-2">
                  <RadioGroupItem
                    value={option.value}
                    id={`${channel.id}-scope-${option.value}`}
                    className="mt-0.5"
                  />
                  <div className="min-w-0">
                    <Label
                      htmlFor={`${channel.id}-scope-${option.value}`}
                      className="flex items-center gap-2 text-sm font-normal"
                    >
                      {option.label}
                      {/* Which option the admin default points at, shown on
                          the option itself — so "what happens if I stop
                          choosing" is answerable without leaving the row. */}
                      {isDefault && (
                        <Badge variant="outline" className="text-[10px]">
                          Default
                        </Badge>
                      )}
                    </Label>
                    <p className="text-xs text-muted-foreground">
                      {option.hint}
                    </p>
                  </div>
                </div>
              )
            })}
          </RadioGroup>

          <p className="text-xs text-muted-foreground">
            {channel.agent_scope_inherited
              ? `Following the administrator's default (${scopeLabel(
                  channel.channel_default_agent_scope,
                )}) — it changes if they change it.`
              : `You set this. The administrator's default is ${scopeLabel(
                  channel.channel_default_agent_scope,
                )}.`}
          </p>

          {!channel.agent_scope_inherited && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-6 px-1.5 text-xs text-muted-foreground"
              disabled={isBusy}
              onClick={() => onPatch({ agent_scope: null })}
            >
              <RotateCcw className="mr-1 h-3 w-3" />
              Follow the default again
            </Button>
          )}

          {scope === SCOPE_LIST && (
            <AgentChecklist
              channel={channel}
              agents={agents}
              agentsFailed={agentsFailed}
              agentsLoading={agentsLoading}
              agentsTruncated={agentsTruncated}
              selectedAgentIds={selectedAgentIds}
              disabled={isBusy}
              onChange={(ids) => onPatch({ agent_ids: ids })}
            />
          )}

          {scope === SCOPE_NONE && (
            <p className="text-xs text-muted-foreground">
              Nothing will route to you on this channel. Picking agents has no
              effect while this is set to None — switch to "Only the agents I
              pick" first.
            </p>
          )}

          {/* The only way back to *pure* inheritance once anything has been
              written — the per-field buttons above clear one field each, but a
              row can exist with every field already cleared. Offered only when
              a row actually exists, because DELETE is a no-op otherwise. */}
          {channel.has_settings && (
            <div className="border-t pt-3">
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                disabled={isBusy}
                onClick={onRequestReset}
              >
                <RotateCcw className="mr-1.5 h-3 w-3" />
                Discard all my settings for this channel
              </Button>
              <p className="mt-1.5 text-xs text-muted-foreground">
                Removes your switch and agent choices, and follows the
                administrator's defaults from then on.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function AgentChecklist({
  channel,
  agents,
  agentsFailed,
  agentsLoading,
  agentsTruncated,
  selectedAgentIds,
  disabled,
  onChange,
}: {
  channel: UserChannelPublic
  agents: AgentOption[]
  agentsFailed: boolean
  agentsLoading: boolean
  agentsTruncated: boolean
  selectedAgentIds: string[]
  disabled: boolean
  onChange: (ids: string[]) => void
}) {
  if (agentsFailed) {
    return (
      <Alert variant="destructive">
        <AlertTriangle className="h-4 w-4" />
        <AlertDescription>
          Couldn't load your agents, so this list is incomplete — don't change
          it until it loads, or you'd save a selection built from a partial
          list.
        </AlertDescription>
      </Alert>
    )
  }

  // Before this branch existed, an in-flight fetch rendered as the positive
  // claim "You don't own any agents yet." — a statement about the user's
  // account made from a request that had not answered. It fires on every first
  // expand, so it is the most-seen lie the card could tell.
  if (agentsLoading) {
    return (
      <div className="space-y-1.5">
        <Skeleton className="h-8 w-full" />
        <Skeleton className="h-8 w-full" />
      </div>
    )
  }

  if (agents.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        You don't own any agents yet.
      </p>
    )
  }

  const toggle = (agentId: string, checked: boolean) =>
    onChange(
      checked
        ? [...selectedAgentIds, agentId]
        : selectedAgentIds.filter((id) => id !== agentId),
    )

  return (
    <div className="space-y-2">
      <div className="max-h-52 space-y-1.5 overflow-y-auto rounded-md border p-2">
        {agents.map((agent) => {
          const id = `${channel.id}-agent-${agent.id}`
          return (
            <div key={agent.id} className="flex items-center gap-2">
              <Checkbox
                id={id}
                disabled={disabled}
                checked={selectedAgentIds.includes(agent.id)}
                onCheckedChange={(checked) =>
                  toggle(agent.id, checked === true)
                }
              />
              <Label htmlFor={id} className="text-sm font-normal">
                {agent.name}
              </Label>
            </div>
          )
        })}
      </div>
      {agentsTruncated && (
        <p className="text-xs text-muted-foreground">
          Only your first {agents.length} agents are listed. Any picked agent
          beyond them is still saved and still counted above, but cannot be
          un-picked here.
        </p>
      )}
      {selectedAgentIds.length === 0 && (
        <Alert>
          <AlertTriangle className="h-4 w-4" />
          <AlertDescription>
            No agents picked, so nothing will route to you on this channel.
          </AlertDescription>
        </Alert>
      )}
    </div>
  )
}
