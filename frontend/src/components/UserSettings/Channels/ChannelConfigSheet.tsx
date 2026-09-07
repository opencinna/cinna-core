/**
 * Configure <channel> — the whole of one channel's settings, in a Sheet.
 *
 * Two sections: whether this channel may leave my workspace at all, and which
 * of MY agents it may use. They are separated because they answer different
 * questions; an unheaded run of controls would read as one setting with a
 * strange switch at the bottom.
 *
 * IDENTITY ROUTING COMES FIRST BECAUSE ITS COPY IS A SAFEGUARD
 * -----------------------------------------------------------
 * That section has a fixed height (one switch, three short paragraphs); the
 * agent section's height is data-driven — a `max-h-[40vh]` checklist plus its
 * warnings. With the agents first, a user with enough agents got a Sheet whose
 * initial view showed the consent *switch* with its consequence paragraph
 * below the fold: the same failure as putting that paragraph in a tooltip,
 * reached by scroll position instead. Fixed-height consent first; no agent
 * count can push it out of view.
 *
 * AUTO-SAVE, DELIBERATELY, ON EVERY CONTROL
 * -----------------------------------------
 * The guidelines' Edit story asks for Save/Cancel with dirty tracking. This
 * surface does not have it, for two reasons specific to it:
 *
 *  1. A dirty-tracked buffer would have to hold a client-side pending
 *     resolution of three fields whose `null` means "revert to the
 *     administrator's default" — a second implementation of the inherit rules,
 *     which is the one thing this feature is organised against (see
 *     `channelScopes.ts`).
 *  2. `allow_identity_routing` is an audited consent transition
 *     (`SERVER_CHANNEL_IDENTITY_ROUTING_CHANGED`). A switch that acts when
 *     flipped matches a consent; a Save button that commits that consent
 *     together with unrelated agent picks does not.
 *
 * One model, applied to every control here, so the surface still holds exactly
 * one interaction model.
 *
 * IDENTITY ROUTING IS THE ONE SETTING THAT IS NOT ABOUT THIS USER'S AGENTS
 * -----------------------------------------------------------------------
 * `allow_identity_routing` lets the channel route a message to *another
 * person's* agent, which means the conversation is created in that person's
 * workspace and is readable by them. So it is off by default, it never
 * inherits from an administrator default (an admin must not consent to that on
 * someone's behalf), and its copy states the consequence in the switch itself
 * rather than in a tooltip — it is not obvious, and it cannot be undone after
 * the message is sent. THE COPY IS THE FEATURE: it is shown whether the switch
 * is on or off, because a warning that only appears after you have opted in is
 * a warning delivered too late.
 *
 * ONE IDENTITY TOGGLE, NOT TWO
 * ----------------------------
 * The per-person switches are NOT here. They are
 * `IdentityBindingAssignment.is_enabled`, person-level, read and written
 * through `/users/me/identity-contacts/` — the same toggle Identity MCP uses,
 * deliberately reused rather than duplicated per channel. A per-channel
 * identity allowlist would be a second source of truth for "may I address this
 * person's identity", and the two would drift. Because they are person-level
 * they live on their own card, and this section says where.
 */
import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { AlertTriangle, RotateCcw } from "lucide-react"

import type { UserChannelPublic } from "@/client"
import { AgentsService, IdentityContactsService } from "@/client"
import { getChannelTypeMeta } from "@/components/Admin/ServerChannels/channelTypes"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Separator } from "@/components/ui/separator"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  IDENTITY_CONTACTS_KEY,
  inheritCaption,
  isBlockedByAdmin,
  SCOPE_LIST,
  SCOPE_NONE,
  SCOPE_OPTIONS,
  scopeLabel,
  UNAVAILABLE_SENTENCE,
} from "./channelScopes"
import { useChannelUpdate } from "./useChannelUpdate"

/** `GET /agents/` has no name filter, so the checklist is capped and says so. */
const AGENT_PAGE_SIZE = 200

/**
 * The meta `getChannelTypeMeta` hands back for a type with no entry of its own.
 *
 * It is one shared object, so reference equality answers "is this type's copy
 * written for a reader?" — the fallback's tagline tells an administrator to
 * configure raw JSON, which is not a sentence to show a user under a channel
 * name. A backend adapter registered without a `channelTypes.ts` entry still
 * works everywhere else; it just gets no subtitle here.
 */
const FALLBACK_TYPE_META = getChannelTypeMeta("__unregistered__")

interface ChannelConfigSheetProps {
  /**
   * Read from the `["userChannels"]` cache by the host on every render, never
   * copied into state: each write answers with the re-resolved row, and a
   * snapshot taken when the Sheet opened would show the pre-write value.
   */
  channel: UserChannelPublic
  onClose: () => void
}

export function ChannelConfigSheet({
  channel,
  onClose,
}: ChannelConfigSheetProps) {
  const meta = getChannelTypeMeta(channel.channel_type)
  const scope = channel.agent_scope
  const selectedAgentIds = channel.agent_ids ?? []
  const blockedByAdmin = isBlockedByAdmin(channel)
  // The scope vocabulary is an open string column that can grow without a
  // client release, so this Sheet has to admit when it is looking at a value
  // it cannot edit: a `RadioGroup` with no matching option renders as "nothing
  // is set", and the user's first click would silently overwrite a scope this
  // version never showed them.
  const scopeIsKnown = SCOPE_OPTIONS.some((o) => o.value === scope)

  // Both queries are unconditional because this component is mounted only
  // while the Sheet is open.
  const {
    data: agentsData,
    isLoading: agentsLoading,
    isError: agentsFailed,
    error: agentsError,
    refetch: refetchAgents,
  } = useQuery({
    queryKey: ["allAgents"],
    queryFn: () =>
      AgentsService.readAgents({ skip: 0, limit: AGENT_PAGE_SIZE }),
  })
  const agents = agentsData?.data ?? []
  // `count` here IS a real total (`GET /agents/` returns one). Used only to
  // admit that the checklist is truncated — never rendered as the number of
  // agents shown.
  const agentsTruncated = (agentsData?.count ?? 0) > agents.length

  const { data: contacts, isError: contactsFailed } = useQuery({
    queryKey: IDENTITY_CONTACTS_KEY,
    queryFn: () => IdentityContactsService.listIdentityContacts(),
  })

  // The row writes through the same hook, so a switch flipped on the card and
  // a tick made here queue against each other instead of racing.
  const updateMutation = useChannelUpdate(channel.id)
  const isBusy = updateMutation.isPending

  // Each tick sends the whole list, built from the cache-written row. Do NOT
  // debounce or batch this: a later payload rebuilt from a stale selection
  // silently drops an earlier tick.
  const toggleAgent = (agentId: string, checked: boolean) =>
    updateMutation.mutate({
      agent_ids: checked
        ? [...selectedAgentIds, agentId]
        : selectedAgentIds.filter((id) => id !== agentId),
    })

  // A claim about other people must never be made from a request that did not
  // answer, so a failed contacts fetch renders no warning at all.
  const identityInert =
    !contactsFailed &&
    channel.allow_identity_routing &&
    (contacts?.length ?? 0) > 0 &&
    !contacts?.some((c) => c.is_enabled)

  return (
    <Sheet
      open
      onOpenChange={(next) => {
        if (!next && !isBusy) onClose()
      }}
    >
      <SheetContent
        side="right"
        className="w-full sm:max-w-lg"
        // A write in flight owns the Sheet: closing it would leave the pending
        // state with nowhere to live.
        onEscapeKeyDown={(e) => {
          if (isBusy) e.preventDefault()
        }}
        onInteractOutside={(e) => {
          if (isBusy) e.preventDefault()
        }}
      >
        <SheetHeader>
          <SheetTitle>{channel.name}</SheetTitle>
          <SheetDescription>
            {meta === FALLBACK_TYPE_META ? channel.channel_type : meta.tagline}
          </SheetDescription>
        </SheetHeader>

        <div className="flex-1 space-y-4 overflow-y-auto px-4">
          {blockedByAdmin && (
            <Alert>
              <AlertTriangle className="h-4 w-4" />
              <AlertDescription>{UNAVAILABLE_SENTENCE}</AlertDescription>
            </Alert>
          )}

          <section className="space-y-3">
            <h3 className="text-sm font-medium">Identity routing</h3>

            <div className="flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <Label
                  htmlFor={`${channel.id}-identity-routing`}
                  className="text-sm font-normal"
                >
                  Let this channel reach agents other people have shared with me
                </Label>
                <p className="mt-1 text-xs text-muted-foreground">
                  Lets you address a colleague by name on this channel — "ask HR
                  about my time off" — and reach an agent they have shared,
                  instead of one of your own.
                </p>
                {/* Not a tooltip, not conditional on the switch being on. This
                    is the part people would not guess, and it is the part they
                    cannot undo afterwards. */}
                <p className="mt-1 text-xs text-muted-foreground">
                  <span className="font-medium text-foreground">
                    Your message and the whole conversation then live in that
                    person's workspace, and they can read it.
                  </span>{" "}
                  Switching this off stops future messages; it does not take
                  back one already sent.
                </p>
              </div>
              <Switch
                id={`${channel.id}-identity-routing`}
                checked={channel.allow_identity_routing ?? false}
                disabled={isBusy}
                aria-label={`Allow identity routing on ${channel.name}`}
                onCheckedChange={(next) =>
                  updateMutation.mutate({ allow_identity_routing: next })
                }
              />
            </div>

            {/* No inherited-default caption here, unlike every other field on
                this surface, and the absence is deliberate: this setting has
                no channel default to follow. An administrator cannot consent
                on someone's behalf to their conversations being readable by a
                third person, so it is per-user, off until set, and offers no
                "follow the default" path back. */}
            <p className="text-xs text-muted-foreground">
              This is yours alone — administrators have no default for it, and
              it stays off until you turn it on.
            </p>

            {/* Named, not linked: the card is on this same page, and closing
                this Sheet reveals it. */}
            <p className="text-xs text-muted-foreground">
              Who you can address by name is set under{" "}
              <span className="font-medium text-foreground">
                People who shared with you
              </span>{" "}
              on this tab — those switches are per person, not per channel.
            </p>

            {identityInert && (
              <Alert>
                <AlertTriangle className="h-4 w-4" />
                <AlertDescription>
                  Identity routing is on for this channel, but everyone is
                  switched off — so nothing will route to another person's
                  agent.
                </AlertDescription>
              </Alert>
            )}
          </section>

          <Separator />

          <section className="space-y-3">
            <h3 className="text-sm font-medium">My agents</h3>

            <RadioGroup
              value={scope}
              disabled={isBusy}
              onValueChange={(next) =>
                updateMutation.mutate({ agent_scope: next })
              }
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
                            choosing" is answerable here. */}
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

            {!scopeIsKnown && (
              <Alert>
                <AlertTriangle className="h-4 w-4" />
                <AlertDescription>
                  This channel is set to <strong>{scope}</strong>, which this
                  version of the app doesn't know how to show. Picking one of
                  the options above replaces it.
                </AlertDescription>
              </Alert>
            )}

            {/* The honest caption. An inherited value names the default it is
                following and says it will keep following it; an explicit one
                says the default no longer applies. */}
            <p className="text-xs text-muted-foreground">
              {inheritCaption(
                channel.agent_scope_inherited,
                scopeLabel(channel.channel_default_agent_scope),
              )}
            </p>

            {!channel.agent_scope_inherited && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-6 px-1.5 text-xs text-muted-foreground"
                disabled={isBusy}
                onClick={() => updateMutation.mutate({ agent_scope: null })}
              >
                <RotateCcw className="mr-1 h-3 w-3" />
                Follow the default again
              </Button>
            )}

            {scope === SCOPE_LIST && (
              <div className="space-y-2">
                {agentsFailed ? (
                  <QueryErrorAlert
                    error={agentsError}
                    fallback="Couldn't load your agents"
                    onRetry={() => refetchAgents()}
                  >
                    <p className="text-xs">
                      This list is incomplete — don't change it until it loads,
                      or you'd save a selection built from a partial list.
                    </p>
                  </QueryErrorAlert>
                ) : agentsLoading ? (
                  // Before this branch existed, an in-flight fetch rendered as
                  // the positive claim "You don't own any agents yet." — a
                  // statement about the user's account made from a request
                  // that had not answered.
                  <div className="space-y-1.5">
                    <Skeleton className="h-8 w-full" />
                    <Skeleton className="h-8 w-full" />
                  </div>
                ) : agents.length === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    You don't own any agents yet.{" "}
                    <Link to="/agents" className="text-primary hover:underline">
                      Create one
                    </Link>
                    .
                  </p>
                ) : (
                  <>
                    <div className="max-h-[40vh] space-y-1.5 overflow-y-auto rounded-md border p-2">
                      {agents.map((agent) => {
                        const id = `${channel.id}-agent-${agent.id}`
                        return (
                          <div
                            key={agent.id}
                            className="flex items-center gap-2"
                          >
                            <Checkbox
                              id={id}
                              disabled={isBusy}
                              checked={selectedAgentIds.includes(agent.id)}
                              onCheckedChange={(checked) =>
                                toggleAgent(agent.id, checked === true)
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
                        Only your first {agents.length} agents are listed. Any
                        picked agent beyond them is still saved and still
                        counted, but cannot be un-picked here.
                      </p>
                    )}
                    {selectedAgentIds.length === 0 && (
                      <Alert>
                        <AlertTriangle className="h-4 w-4" />
                        <AlertDescription>
                          No agents picked, so nothing will route to you on this
                          channel.
                        </AlertDescription>
                      </Alert>
                    )}
                  </>
                )}
              </div>
            )}

            {scope === SCOPE_NONE && (
              <p className="text-xs text-muted-foreground">
                Nothing will route to you on this channel. Picking agents has no
                effect while this is set to None — switch to "Only the agents I
                pick" first.
              </p>
            )}
          </section>
        </div>

        <SheetFooter>
          <Button variant="outline" disabled={isBusy} onClick={onClose}>
            Close
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}
