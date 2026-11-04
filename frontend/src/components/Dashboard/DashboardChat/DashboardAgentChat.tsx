import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ExternalLink, Loader2, PanelRightClose, SquarePen } from "lucide-react"
import { type ReactNode, useCallback, useState } from "react"

import {
  type AgentPublic,
  type DashboardChatPublic,
  DashboardChatService,
} from "@/client"
import { AgentBadge } from "@/components/Common/AgentBadge"
import { QueryErrorAlert } from "@/components/Common/QueryErrorAlert"
import type { ComponentPrompt } from "@/components/Dashboard/AgentComponents/componentSpec"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import {
  DashboardChatConversation,
  type PendingSend,
  type Seed,
} from "./DashboardChatConversation"

export function dashboardChatQueryKey(dashboardId: string, agentId: string) {
  return ["dashboardChat", dashboardId, agentId] as const
}

function HeaderButton({
  label,
  onClick,
  disabled,
  children,
  href,
}: {
  label: string
  onClick?: () => void
  disabled?: boolean
  children: ReactNode
  href?: string
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {href && !disabled ? (
          <Button variant="ghost" size="icon" className="h-7 w-7" asChild>
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={label}
            >
              {children}
            </a>
          </Button>
        ) : (
          // A disabled button gets no pointer events; the span keeps the tooltip.
          <span className="inline-flex">
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              aria-label={label}
              onClick={onClick}
              disabled={disabled}
            >
              {children}
            </Button>
          </span>
        )}
      </TooltipTrigger>
      <TooltipContent side="bottom" className="text-xs">
        {label}
      </TooltipContent>
    </Tooltip>
  )
}

/** The panel header: whose chat it is, then New chat · Open in Chats · Hide chat. */
export function DashboardChatHeader({
  agent,
  sessionId,
  hasMessages,
  onNewChat,
  newChatPending,
  onHide,
}: {
  agent: AgentPublic | null
  sessionId?: string
  hasMessages?: boolean
  onNewChat?: () => void
  newChatPending?: boolean
  onHide: () => void
}) {
  return (
    <div className="flex h-11 shrink-0 items-center gap-2 border-b px-3">
      <div className="min-w-0 flex-1">
        {agent && <AgentBadge agent={agent} className="max-w-full" />}
      </div>
      {agent && (
        <>
          {newChatPending ? (
            <HeaderButton label="Starting a new chat…" disabled>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            </HeaderButton>
          ) : (
            <HeaderButton
              label="New chat"
              onClick={onNewChat}
              disabled={!onNewChat || !hasMessages}
            >
              <SquarePen className="h-3.5 w-3.5" />
            </HeaderButton>
          )}
          <HeaderButton
            label="Open in Chats"
            href={sessionId ? `/session/${sessionId}` : undefined}
            disabled={!sessionId || !hasMessages}
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </HeaderButton>
        </>
      )}
      <HeaderButton label="Hide chat" onClick={onHide}>
        <PanelRightClose className="h-3.5 w-3.5" />
      </HeaderButton>
    </div>
  )
}

interface Props {
  dashboardId: string
  agent: AgentPublic
  active: boolean
  /** Shown on screen right now (the panel is open and this is its agent). */
  visible: boolean
  seed: Seed | undefined
  onSeed: (text: string) => void
  onHide: () => void
  buildContext: (explained: Set<string>) => string | undefined
  onComposerFocus: () => void
  onMessageSent: () => void
  onTurnEnd: (sessionId: string) => void
  starters: ComponentPrompt[]
  chip: ReactNode
}

/**
 * The panel for one agent: its (dashboard, agent) session, found or made
 * lazily on the first message, and New chat (plan §5.1).
 */
export function DashboardAgentChat({
  dashboardId,
  agent,
  active,
  visible,
  seed,
  onSeed,
  onHide,
  ...conversation
}: Props) {
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const queryKey = dashboardChatQueryKey(dashboardId, agent.id)
  const [pending, setPending] = useState<PendingSend | null>(null)

  const chatQuery = useQuery({
    queryKey,
    queryFn: () =>
      DashboardChatService.getDashboardChat({ dashboardId, agentId: agent.id }),
  })

  const openMutation = useMutation({
    mutationFn: (fresh: boolean) =>
      DashboardChatService.openDashboardChat({
        dashboardId,
        requestBody: { agent_id: agent.id, new: fresh },
      }),
    onSuccess: (chat) => {
      queryClient.setQueryData<DashboardChatPublic>(queryKey, chat)
    },
  })

  const onSendWithoutSession = useCallback(
    (send: PendingSend) => {
      openMutation.mutate(false, {
        onSuccess: () => setPending(send),
        onError: () => {
          showErrorToast("Couldn't start the chat")
          onSeed(send.content)
        },
      })
    },
    [openMutation, onSeed, showErrorToast],
  )

  const newChat = () =>
    openMutation.mutate(true, {
      onError: () => showErrorToast("Couldn't start a new chat"),
    })

  const session = chatQuery.data?.session ?? undefined
  const header = (hasMessages: boolean) => (
    <DashboardChatHeader
      agent={agent}
      sessionId={session?.id}
      hasMessages={hasMessages}
      onNewChat={newChat}
      newChatPending={openMutation.isPending && openMutation.variables === true}
      onHide={onHide}
    />
  )

  return (
    <div className={cn("min-h-0 flex-1 flex-col", active ? "flex" : "hidden")}>
      {chatQuery.isLoading ? (
        <>
          {header(false)}
          <div className="flex-1 space-y-3 p-3">
            <Skeleton className="ml-auto h-10 w-2/3 rounded-lg" />
            <Skeleton className="h-16 w-5/6 rounded-lg" />
            <Skeleton className="ml-auto h-10 w-1/2 rounded-lg" />
          </div>
        </>
      ) : chatQuery.isError ? (
        <>
          {header(false)}
          <div className="flex-1 p-3">
            <QueryErrorAlert
              compact
              error={chatQuery.error}
              fallback="Couldn't load this chat."
              onRetry={() => void chatQuery.refetch()}
            />
          </div>
        </>
      ) : (
        <DashboardChatConversation
          key={session?.id ?? "new"}
          sessionId={session?.id}
          visible={visible}
          agent={agent}
          seed={seed}
          onSeed={onSeed}
          pending={pending}
          onPendingTaken={() => setPending(null)}
          onSendWithoutSession={onSendWithoutSession}
          header={({ hasMessages }) => header(hasMessages)}
          {...conversation}
        />
      )}
    </div>
  )
}
