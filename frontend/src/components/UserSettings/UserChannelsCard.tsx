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
 * IDENTITY ROUTING IS THE ONE SETTING THAT IS NOT ABOUT THIS USER'S AGENTS
 * -----------------------------------------------------------------------
 * `allow_identity_routing` lets the channel route a message to *another
 * person's* agent, which means the conversation is created in that person's
 * workspace and is readable by them. So it is off by default, it never
 * inherits from an administrator default (an admin must not consent to that on
 * someone's behalf), and its copy states the consequence in the switch itself
 * rather than in a tooltip — it is not obvious, and it cannot be undone after
 * the message is sent.
 *
 * ONE IDENTITY TOGGLE, NOT TWO
 * ----------------------------
 * The per-person switches below the master switch are `IdentityBindingAssignment
 * .is_enabled`, read and written through `/users/me/identity-contacts/` — the
 * same person-level toggle Identity MCP uses, deliberately reused rather than
 * duplicated per channel. A per-channel identity allowlist would be a second
 * source of truth for "may I address this person's identity", and the two would
 * drift. The consequence is visible in the UI and must stay stated: those
 * switches are NOT scoped to this channel, and the copy says so.
 *
 * WHICH WAY THE PER-PERSON SWITCH POINTS
 * --------------------------------------
 * It is the **caller's** switch, not the receiver's: the row is keyed by
 * `target_user_id`, and `IdentityService.toggle_identity_contact` filters on
 * `target_user_id == current_user.id`. So it answers "may I address this
 * person?", and turning it off removes them from the reader's OWN reach —
 * it does not stop them reaching the reader. Nothing on this card lets a user
 * control who may reach them; that is the identity owner's `is_active` on the
 * binding and the assignment, edited from the identity-sharing screens. Getting
 * this sentence backwards is not a copy nit: this is the one control on the
 * card whose entire purpose is informed consent, and a user who reads it as a
 * shield will leave it on for exactly the person they meant to block.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Loader2,
  RotateCcw,
  Users,
} from "lucide-react"
import { useState } from "react"
import {
  AgentsService,
  type IdentityContactPublic,
  IdentityContactsService,
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
 * The subset of `UserChannelUpdate` this card is allowed to write.
 *
 * Derived from the generated type rather than hand-declared, so a change to
 * the wire model reaches these call sites — but narrowed with `Pick`, which is
 * what structurally keeps `pinned_agent_id` unwritable here (no UI sets it
 * yet), so a stray patch cannot pin an agent by accident.
 *
 * `allow_identity_routing` joined the list in Phase 3, which is what the
 * Identity routing section writes. `null` stays in the value type for the two
 * inheritable fields, where it is not "no value" but "revert this field to the
 * channel default" — it is NOT a legal value for `allow_identity_routing`,
 * which has no inherited state and is rejected by the API if sent as null, so
 * that section only ever sends a boolean.
 */
type ChannelPatch = Pick<
  UserChannelUpdate,
  "is_enabled" | "agent_scope" | "agent_ids" | "allow_identity_routing"
>

function scopeLabel(scope: string): string {
  // Falls through to the raw value rather than a blank: the scope vocabulary
  // is a plain string column that can grow without a client release.
  return SCOPE_OPTIONS.find((o) => o.value === scope)?.label ?? scope
}

/** The rendered name for an identity contact — never a blank row. */
function contactLabel(contact: IdentityContactPublic): string {
  return contact.owner_name?.trim() || contact.owner_email
}

/**
 * The project-wide ordering for a list of people: the *rendered* name, then id.
 *
 * Uses the same *key* the backend orders by — the rendered name, i.e.
 * `coalesce(nullif(full_name, ''), email)` as `IdentityCandidateProvider`
 * spells it, which is why `contactLabel` falls back on a blank name and not
 * only on a missing one. It is **not** the same ordering: `localeCompare` is
 * locale-aware, the backend sorts under the database collation, and the two
 * disagree on case and accents. That is tolerable because the order here is
 * cosmetic — this list is read, never indexed into, and no backend result is
 * matched against its positions. Do not "fix" it by reaching for a
 * collation-mimicking comparator; if the order ever becomes load-bearing, the
 * answer is to have the server send it in order.
 *
 * The id tiebreak keeps two people with the same display name in a stable
 * order across renders instead of leaving it to the server's row order.
 */
function sortContacts(
  contacts: IdentityContactPublic[],
): IdentityContactPublic[] {
  return [...contacts].sort((a, b) => {
    const byName = contactLabel(a).localeCompare(contactLabel(b))
    return byName !== 0 ? byName : a.owner_id.localeCompare(b.owner_id)
  })
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

  // The people who have shared their identity with this user. Fetched on the
  // same "only once a row is open" terms as the agent list, and under the query
  // key `AppAgentRoutesCard` already uses — this is the same list, viewed from
  // a second surface, and two keys for it would let one card show a person the
  // other has just switched off.
  const {
    data: identityContacts,
    isError: isContactsError,
    isLoading: isContactsLoading,
  } = useQuery({
    queryKey: ["identity-contacts"],
    queryFn: () => IdentityContactsService.listIdentityContacts(),
    enabled: expanded.size > 0,
  })
  const contacts = sortContacts(identityContacts ?? [])

  // Which per-person toggles have a request in flight, tracked here rather
  // than read off `toggleContactMutation.variables`. React Query keeps
  // `variables` for the LATEST call only, so two quick toggles on different
  // people would move the spinner to the second row and leave the first
  // looking settled while its request was still open — and the first row's
  // switch re-enabled, inviting a third write to race the one already in
  // flight. A set of ids is the honest model: these requests are concurrent.
  const [pendingContactIds, setPendingContactIds] = useState<
    ReadonlySet<string>
  >(new Set())

  const toggleContactMutation = useMutation({
    mutationFn: ({
      ownerId,
      isEnabled,
    }: {
      ownerId: string
      isEnabled: boolean
    }) =>
      IdentityContactsService.toggleIdentityContact({
        ownerId,
        requestBody: { is_enabled: isEnabled },
      }),
    onMutate: ({ ownerId }) =>
      setPendingContactIds((prev) => new Set(prev).add(ownerId)),
    // `onSettled`, not `onSuccess`/`onError`: a row must come back out of the
    // pending set on every outcome, including a rejected request.
    onSettled: (_data, _error, { ownerId }) =>
      setPendingContactIds((prev) => {
        const next = new Set(prev)
        next.delete(ownerId)
        return next
      }),
    // Invalidated rather than written into the cache: unlike the channel PUT,
    // this endpoint answers with a bare `Message`, so the client has nothing
    // authoritative to write and guessing would be the start of the drift this
    // card avoids everywhere else.
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["identity-contacts"] }),
    onError: (err) =>
      showErrorToast(getErrorMessage(err, "Failed to update this person")),
  })

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
                  contacts={contacts}
                  contactsFailed={isContactsError}
                  contactsLoading={isContactsLoading}
                  pendingContactIds={pendingContactIds}
                  isOpen={expanded.has(channel.id)}
                  isBusy={isBusy(channel.id)}
                  onToggleOpen={() => toggleExpanded(channel.id)}
                  onPatch={(body) =>
                    updateMutation.mutate({ channelId: channel.id, body })
                  }
                  onToggleContact={(ownerId, isEnabled) =>
                    toggleContactMutation.mutate({ ownerId, isEnabled })
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
              — your on/off switch, your agent choices, and your identity
              routing opt-in, which goes back to off. From then on the channel
              follows the administrator's defaults, including any future change
              to them. The people you have enabled are kept: those are
              person-level and are not this channel's to discard.
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
  contacts: IdentityContactPublic[]
  contactsFailed: boolean
  contactsLoading: boolean
  pendingContactIds: ReadonlySet<string>
  isOpen: boolean
  isBusy: boolean
  onToggleOpen: () => void
  onPatch: (body: ChannelPatch) => void
  onToggleContact: (ownerId: string, isEnabled: boolean) => void
  onRequestReset: () => void
}

function ChannelRow({
  channel,
  agents,
  agentsFailed,
  agentsLoading,
  agentsTruncated,
  contacts,
  contactsFailed,
  contactsLoading,
  pendingContactIds,
  isOpen,
  isBusy,
  onToggleOpen,
  onPatch,
  onToggleContact,
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
        What this channel can reach
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
          {/* Only when on. Identity routing is off by default, so a badge on
              every collapsed row would be noise about a feature most people
              are not using — while the people who ARE need to see it without
              opening anything. */}
          {channel.allow_identity_routing && (
            <Badge variant="secondary" className="text-[10px]">
              + identities
            </Badge>
          )}
        </span>
      </button>

      {isOpen && (
        <div
          id={`${channel.id}-agent-panel`}
          className="space-y-3 border-t px-3 py-3"
        >
          {/* Headed, because the panel now answers two different questions —
              which of MY agents this channel may use, and whether it may leave
              my workspace at all. An unheaded run of controls would read as one
              setting with a strange switch at the bottom. */}
          <p className="text-xs font-medium">My agents</p>

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

          <IdentityRoutingSection
            channel={channel}
            contacts={contacts}
            contactsFailed={contactsFailed}
            contactsLoading={contactsLoading}
            pendingContactIds={pendingContactIds}
            disabled={isBusy}
            onToggleChannel={(next) =>
              onPatch({ allow_identity_routing: next })
            }
            onToggleContact={onToggleContact}
          />

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

/**
 * The Identity routing section: one master switch, then the people it applies to.
 *
 * THE COPY IS THE FEATURE
 * -----------------------
 * Everything else on this card is reversible and local. This switch is neither:
 * once a message has routed to someone's identity, the conversation exists in
 * their workspace and they can read it, and switching back off does not
 * retract it. So the consequence is stated on the switch, in plain words, and
 * shown whether the switch is on or off — a warning that only appears after you
 * have opted in is a warning delivered too late.
 *
 * WHY THE PER-PERSON SWITCHES STAY LIVE WHEN THE MASTER SWITCH IS OFF
 * -------------------------------------------------------------------
 * They are not this channel's settings. `IdentityBindingAssignment.is_enabled`
 * is person-level and governs every surface identity reaches, so disabling them
 * here would misrepresent them as scoped to the channel, and toggling one would
 * silently change behaviour elsewhere. They are shown with the master switch
 * off as well, because "who could I reach if I turned this on" is the question
 * that decides whether to turn it on.
 */
function IdentityRoutingSection({
  channel,
  contacts,
  contactsFailed,
  contactsLoading,
  pendingContactIds,
  disabled,
  onToggleChannel,
  onToggleContact,
}: {
  channel: UserChannelPublic
  contacts: IdentityContactPublic[]
  contactsFailed: boolean
  contactsLoading: boolean
  pendingContactIds: ReadonlySet<string>
  disabled: boolean
  onToggleChannel: (next: boolean) => void
  onToggleContact: (ownerId: string, isEnabled: boolean) => void
}) {
  const allowed = channel.allow_identity_routing ?? false
  const switchId = `${channel.id}-identity-routing`

  return (
    <div className="space-y-3 border-t pt-3">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <Label
            htmlFor={switchId}
            className="flex items-center gap-2 text-xs font-medium"
          >
            <Users className="h-3.5 w-3.5" />
            Identity routing
          </Label>
          <p className="mt-1 text-xs text-muted-foreground">
            Lets you address a colleague by name on this channel — "ask HR about
            my time off" — and reach an agent they have shared, instead of one
            of your own.
          </p>
          {/* Not a tooltip, not conditional on the switch being on. This is the
              part people would not guess, and it is the part they cannot undo
              afterwards. */}
          <p className="mt-1 text-xs text-muted-foreground">
            <span className="font-medium text-foreground">
              Your message and the whole conversation then live in that person's
              workspace, and they can read it.
            </span>{" "}
            Switching this off stops future messages; it does not take back one
            already sent.
          </p>
        </div>
        <Switch
          id={switchId}
          checked={allowed}
          disabled={disabled}
          aria-label={`Allow identity routing on ${channel.name}`}
          onCheckedChange={onToggleChannel}
        />
      </div>

      {/* No inherited-default caption here, unlike every other field on this
          card, and the absence is deliberate: this setting has no channel
          default to follow. An administrator cannot consent on someone's
          behalf to their conversations being readable by a third person, so it
          is per-user, off until set, and offers no "follow the default" path
          back. */}
      <p className="text-xs text-muted-foreground">
        This is yours alone — administrators have no default for it, and it
        stays off until you turn it on.
      </p>

      <div className="space-y-2">
        <p className="text-xs font-medium">People who shared with you</p>

        {contactsFailed ? (
          /* Same rule as the agent checklist: a failed request must never
             render as "nobody has shared with you", which is a claim about
             other people made from a request that did not answer. */
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" />
            <AlertDescription>
              Couldn't load the people who have shared their identity with you.
              This is a failed request, not an empty list.
            </AlertDescription>
          </Alert>
        ) : contactsLoading ? (
          <div className="space-y-1.5">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : contacts.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            Nobody has shared their identity with you yet. When someone does,
            they appear here and you choose whether to keep them on.
          </p>
        ) : (
          <>
            <div className="max-h-52 space-y-1.5 overflow-y-auto rounded-md border p-2">
              {contacts.map((contact) => {
                const label = contactLabel(contact)
                const id = `${channel.id}-identity-${contact.owner_id}`
                return (
                  <div
                    key={contact.owner_id}
                    className="flex items-center gap-2"
                  >
                    <div className="min-w-0 flex-1">
                      <Label htmlFor={id} className="text-sm font-normal">
                        {label}
                      </Label>
                      <p className="truncate text-xs text-muted-foreground">
                        {/* The email is shown alongside the name because
                            "which Alex is this?" has to be answerable before
                            deciding to let them read a conversation. It is
                            skipped when it IS the name, to avoid a row that
                            says the same thing twice. */}
                        {label !== contact.owner_email &&
                          `${contact.owner_email} · `}
                        {contact.agent_count === 1
                          ? "1 agent"
                          : `${contact.agent_count} agents`}
                      </p>
                    </div>
                    {pendingContactIds.has(contact.owner_id) && (
                      <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
                    )}
                    <Switch
                      id={id}
                      checked={contact.is_enabled}
                      disabled={pendingContactIds.has(contact.owner_id)}
                      aria-label={`Let me address ${label} by name`}
                      onCheckedChange={(next) =>
                        onToggleContact(contact.owner_id, next)
                      }
                    />
                  </div>
                )
              })}
            </div>
            <p className="text-xs text-muted-foreground">
              These switches decide who{" "}
              <span className="font-medium text-foreground">you</span> can
              address by name — they do not control who can reach you. They are
              per person, not per channel, so turning someone off here also
              stops you addressing them anywhere else identity is used.
            </p>
          </>
        )}

        {allowed &&
          contacts.length > 0 &&
          !contacts.some((c) => c.is_enabled) && (
            <Alert>
              <AlertTriangle className="h-4 w-4" />
              <AlertDescription>
                Identity routing is on for this channel, but everyone is
                switched off — so nothing will route to another person's agent.
              </AlertDescription>
            </Alert>
          )}
      </div>
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
