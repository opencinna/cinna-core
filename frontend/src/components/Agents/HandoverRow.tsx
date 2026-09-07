import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  Bot,
  EllipsisVertical,
  Pencil,
  Power,
  PowerOff,
  Trash2,
} from "lucide-react"

import type { HandoverConfigPublic } from "@/client"
import { AgentsService } from "@/client"
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
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import { handleError } from "@/utils"
import { getColorPreset } from "@/utils/colorPresets"
import { EditHandoverPromptModal } from "./EditHandoverPromptModal"

interface HandoverRowProps {
  /** The agent that hands work over (the card's own agent). */
  agentId: string
  handover: HandoverConfigPublic
  /** Colour preset of the target agent, used to tint the row's icon tile. */
  targetColorPreset?: string | null
}

/**
 * One handover, as a compact P3 row.
 *
 * Rendered by both the card and the "Show all" Sheet so the two hosts cannot
 * drift. Every mutation lives one level down — in the `⋯` menu, the edit
 * dialog or the delete confirm — so the row itself shows no inline action and
 * nothing on it can change the card's height.
 *
 * The edit dialog and the delete confirm are owned by the row rather than by
 * the host: that keeps the Sheet open behind them, and keeps the card and the
 * Sheet on one code path.
 */
export function HandoverRow({
  agentId,
  handover,
  targetColorPreset,
}: HandoverRowProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [editOpen, setEditOpen] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)

  const colorPreset = getColorPreset(targetColorPreset)

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["agentHandovers", agentId] })

  const toggleMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      AgentsService.updateHandoverConfig({
        id: agentId,
        handoverId: handover.id,
        requestBody: { enabled },
      }),
    onSuccess: (_data, enabled) => {
      showSuccessToast(
        enabled
          ? `Handover to ${handover.target_agent_name} enabled`
          : `Handover to ${handover.target_agent_name} disabled`,
      )
      invalidate()
    },
    onError: handleError.bind(showErrorToast),
  })

  const deleteMutation = useMutation({
    mutationFn: () =>
      AgentsService.deleteHandoverConfig({
        id: agentId,
        handoverId: handover.id,
      }),
    onSuccess: () => {
      showSuccessToast(`Handover to ${handover.target_agent_name} deleted`)
      setConfirmOpen(false)
      invalidate()
    },
    onError: handleError.bind(showErrorToast),
  })

  // Pending is scoped to this row: only the row being mutated loses its menu.
  const isPending = toggleMutation.isPending || deleteMutation.isPending

  const promptPreview =
    handover.handover_prompt.replace(/\s+/g, " ").trim() || "No prompt yet"

  return (
    <div
      className={cn(
        "flex items-center justify-between px-3 py-2 border rounded-lg",
        !handover.enabled && "opacity-60",
      )}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <div
            className={cn(
              "w-6 h-6 rounded-md shrink-0 flex items-center justify-center",
              colorPreset.iconBg,
            )}
          >
            <Bot className={cn("h-3.5 w-3.5", colorPreset.iconText)} />
          </div>
          <span className="text-sm font-medium truncate min-w-0">
            {handover.target_agent_name}
          </span>
          {!handover.enabled && (
            <Badge variant="secondary" className="text-xs shrink-0">
              Off
            </Badge>
          )}
        </div>
        <p className="text-xs text-muted-foreground truncate mt-0.5">
          {promptPreview}
        </p>
      </div>

      <div className="flex items-center gap-0.5 shrink-0">
        <DropdownMenu>
          <Tooltip>
            <TooltipTrigger asChild>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  disabled={isPending}
                  aria-label={`Actions for the handover to ${handover.target_agent_name}`}
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
                setEditOpen(true)
              }}
            >
              <Pencil />
              Edit prompt
            </DropdownMenuItem>
            <DropdownMenuItem
              onSelect={() => toggleMutation.mutate(!handover.enabled)}
            >
              {handover.enabled ? <PowerOff /> : <Power />}
              {handover.enabled ? "Disable" : "Enable"}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              variant="destructive"
              onSelect={(e) => {
                e.preventDefault()
                setConfirmOpen(true)
              }}
            >
              <Trash2 />
              Delete handover
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {/* Mounted only while open: a resident instance would re-seed its form
          from a background refetch and lose the user's in-progress edit. */}
      {editOpen && (
        <EditHandoverPromptModal
          agentId={agentId}
          handoverId={handover.id}
          targetAgentId={handover.target_agent_id}
          targetAgentName={handover.target_agent_name}
          currentPrompt={handover.handover_prompt}
          open
          onClose={() => setEditOpen(false)}
        />
      )}

      <AlertDialog
        open={confirmOpen}
        // Escape would otherwise close the confirm mid-request, leaving the
        // pending state with nowhere to live.
        onOpenChange={(next) => {
          if (!deleteMutation.isPending) setConfirmOpen(next)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete handover</AlertDialogTitle>
            <AlertDialogDescription>
              Remove the handover to{" "}
              <strong>{handover.target_agent_name}</strong>? This agent will no
              longer pass work to it. This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                // Keep the confirm open while the request is in flight so the
                // pending state has somewhere to live.
                e.preventDefault()
                deleteMutation.mutate()
              }}
              disabled={deleteMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteMutation.isPending ? "Deleting…" : "Delete"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
