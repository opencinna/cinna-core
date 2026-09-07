import {
  Bot,
  Check,
  Copy,
  KeyRound,
  MessageCircle,
  Pencil,
  Power,
  PowerOff,
  Trash2,
  Users,
  Wrench,
} from "lucide-react"

import { ListRow, RowFlag } from "@/components/Common/ListRow"
import { RowActionsMenu } from "@/components/Common/RowActionsMenu"
import { Button } from "@/components/ui/button"
import {
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

/**
 * Structural, not the generated `MCPConnectorPublic`: the card fetches these
 * through a hand-rolled `fetch` against a local interface, and the row needs
 * only the fields it renders.
 */
export interface McpConnectorRowConnector {
  id: string
  name: string
  mode: string
  is_active: boolean
  is_agent_to_agent?: boolean
  allowed_emails?: string[] | null
  allowed_user_ids?: string[] | null
  allow_token_access?: boolean | null
}

interface McpConnectorRowProps {
  connector: McpConnectorRowConnector
  /** Agent-to-agent connectors get the `Bot` tile and their own delete copy. */
  isAgentToAgent?: boolean
  /** `null` when `MCP_SERVER_BASE_URL` is unset — Copy is then disabled. */
  serverUrl: string | null
  /** This row's URL was copied a moment ago. */
  copied: boolean
  onCopyUrl: (url: string) => void
  onEdit: () => void
  onToggleActive: (next: boolean) => void
  onDelete: () => void
  /** A write on this connector is in flight. */
  isPending?: boolean
}

/**
 * One MCP connector, as a house list row — used by both lists on the card, the
 * direct connectors and the agent-to-agent ones, which were duplicated JSX
 * differing only in a tile and one sentence of delete copy.
 *
 * What the row dropped: an "Active"/"Inactive" `Badge` pair (now the dot), an
 * "Agent to Agent" `Badge` (now the tile, since it is what the row *is*), a
 * user count and a "Tokens" `Badge` (now flags), and two of its four inline
 * buttons — Edit and Deactivate went to the menu. Copy stays inline because
 * handing the URL to an MCP client is the reason anyone opens this card.
 */
export function McpConnectorRow({
  connector,
  isAgentToAgent = false,
  serverUrl,
  copied,
  onCopyUrl,
  onEdit,
  onToggleActive,
  onDelete,
  isPending,
}: McpConnectorRowProps) {
  const userCount =
    (connector.allowed_user_ids?.length || 0) +
    (connector.allowed_emails?.length || 0)

  return (
    <ListRow
      muted={!connector.is_active}
      status={{
        tone: connector.is_active ? "on" : "off",
        label: connector.is_active ? "Active" : "Inactive",
      }}
      icon={
        isAgentToAgent ? (
          <span className="flex h-6 w-6 items-center justify-center rounded-md bg-muted">
            <Bot className="h-3.5 w-3.5 text-muted-foreground" />
          </span>
        ) : undefined
      }
      title={connector.name}
      // No metadata line: the tile already says agent-to-agent, and every
      // other fact on this row is a flag. The list stays one line per row.
      flags={
        <>
          <RowFlag
            icon={connector.mode === "building" ? Wrench : MessageCircle}
            label={
              connector.mode === "building"
                ? "Building mode — may change the agent"
                : "Conversation only"
            }
            tone={connector.mode === "building" ? "warning" : "neutral"}
          />
          {userCount > 0 && (
            <RowFlag
              icon={Users}
              label={`Restricted to ${userCount} user${
                userCount === 1 ? "" : "s"
              }`}
            />
          )}
          {connector.allow_token_access && (
            <RowFlag
              icon={KeyRound}
              tone="warning"
              label="Direct tokens may connect to this connector"
            />
          )}
        </>
      }
    >
      {/* Handing the URL to an MCP client is the reason anyone opens this
          card, so Copy is the row's one inline action. */}
      <Tooltip>
        <TooltipTrigger asChild>
          <span>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              disabled={!serverUrl}
              aria-label={`Copy the MCP server URL for ${connector.name}`}
              onClick={() => serverUrl && onCopyUrl(serverUrl)}
            >
              {copied ? (
                <Check className="h-3.5 w-3.5 text-success" />
              ) : (
                <Copy className="h-3.5 w-3.5" />
              )}
            </Button>
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs">
          {serverUrl
            ? copied
              ? "Copied"
              : "Copy MCP server URL"
            : "MCP_SERVER_BASE_URL is not configured"}
        </TooltipContent>
      </Tooltip>

      <RowActionsMenu
        label={`the connector ${connector.name}`}
        disabled={isPending}
      >
        <DropdownMenuItem onSelect={onEdit}>
          <Pencil />
          Edit connector
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onToggleActive(!connector.is_active)}>
          {connector.is_active ? <PowerOff /> : <Power />}
          {connector.is_active ? "Deactivate" : "Activate"}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          variant="destructive"
          onSelect={(e) => {
            e.preventDefault()
            onDelete()
          }}
        >
          <Trash2 />
          Delete connector
        </DropdownMenuItem>
      </RowActionsMenu>
    </ListRow>
  )
}
