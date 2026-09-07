import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  EllipsisVertical,
  Key,
  Pencil,
  ShieldCheck,
  Star,
  Trash2,
} from "lucide-react"
import { useState } from "react"

import type { AICredentialPublic } from "@/client"
import { AiCredentialsService } from "@/client"
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
import { AffectedEnvironmentsDialog } from "../AffectedEnvironmentsDialog"
import { DeleteAICredentialDialog } from "../DeleteAICredentialDialog"
import { describeExpiry, getTypeDisplayName } from "./credentialTypes"
import { EditAICredentialDialog } from "./EditAICredentialDialog"

interface AICredentialRowProps {
  credential: AICredentialPublic
}

/**
 * One AI credential, as a compact P3 row.
 *
 * Rendered by both the card and the "Show all" Sheet so the two hosts cannot
 * drift. The row is a View surface: the star, the "Managed" badge and the
 * expiry badge are passive, and every mutation — Set as default, Edit, Delete
 * — lives in the `⋯` menu. That is the fix for anti-pattern A5, which named
 * this file for its star + pencil + trash on every row.
 *
 * The edit dialog, the delete confirm and the affected-environments dialog are
 * owned here rather than by the host: that keeps the Sheet open behind them and
 * keeps the card and the Sheet on one code path.
 */
export function AICredentialRow({ credential }: AICredentialRowProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [editOpen, setEditOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [affectedOpen, setAffectedOpen] = useState(false)

  const setDefaultMutation = useMutation({
    mutationFn: () =>
      AiCredentialsService.setAiCredentialDefault({
        credentialId: credential.id,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsList"] })
      queryClient.invalidateQueries({ queryKey: ["aiCredentialsStatus"] })
      queryClient.invalidateQueries({ queryKey: ["resolveDefaultCredential"] })
      showSuccessToast("Default credential updated")
      // Chained, not nested: the mutation has settled and no dialog is open
      // when this one appears (guidelines §2 "Disclosure depth").
      setAffectedOpen(true)
    },
    onError: () => showErrorToast("Failed to set default credential"),
  })

  const expiry = describeExpiry(credential.expiry_notification_date)
  const isExpired = expiry?.badge?.variant === "destructive"

  // ≤ 2 facts joined by " · " (P3). The type always leads; the second fact is
  // whichever of the three below applies first. Everything else — the model,
  // the discovery state, the exact expiry — stays in the edit dialog or in a
  // badge tooltip.
  const secondFact =
    credential.type === "openai_compatible" && credential.base_url
      ? credential.base_url
      : credential.is_admin_managed && credential.default_model
        ? `Default model: ${credential.default_model}`
        : (expiry?.fact ?? null)

  const canEdit = !credential.is_admin_managed
  const canSetDefault = !credential.is_default
  const hasMenu = canEdit || canSetDefault

  return (
    <div
      className={cn(
        "flex items-center justify-between px-3 py-2 border rounded-lg",
        isExpired && "opacity-60",
      )}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <div className="w-6 h-6 rounded-md shrink-0 flex items-center justify-center bg-muted">
            <Key className="h-3.5 w-3.5 text-muted-foreground" />
          </div>
          <span className="text-sm font-medium truncate min-w-0">
            {credential.name}
          </span>
          {credential.is_default && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Star className="h-3.5 w-3.5 shrink-0 text-amber-500 fill-amber-500" />
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                {/* One default per *type*, so several rows wear a star at once
                    and an unqualified "Default credential" reads as a
                    contradiction. */}
                Default {getTypeDisplayName(credential.type)} credential
              </TooltipContent>
            </Tooltip>
          )}
          {credential.is_admin_managed && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="secondary" className="shrink-0">
                  <ShieldCheck className="h-3 w-3" />
                  Managed
                </Badge>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-64 text-xs">
                Managed by your administrator — you can use it and set it as
                default, but it can&apos;t be edited or deleted here.
              </TooltipContent>
            </Tooltip>
          )}
          {expiry?.badge && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant={expiry.badge.variant} className="shrink-0">
                  {expiry.badge.label}
                </Badge>
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                {expiry.tooltip}
              </TooltipContent>
            </Tooltip>
          )}
        </div>
        <p className="text-xs text-muted-foreground truncate mt-0.5">
          {getTypeDisplayName(credential.type)}
          {secondFact ? ` · ${secondFact}` : ""}
        </p>
      </div>

      <div className="flex items-center gap-0.5 shrink-0">
        {hasMenu ? (
          <DropdownMenu>
            <Tooltip>
              <TooltipTrigger asChild>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    // Pending is scoped to this row: one credential's
                    // set-default never disables another's menu.
                    disabled={setDefaultMutation.isPending}
                    aria-label={`Actions for ${credential.name}`}
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
              {canSetDefault && (
                <DropdownMenuItem onSelect={() => setDefaultMutation.mutate()}>
                  <Star />
                  Set as default
                </DropdownMenuItem>
              )}
              {canEdit && (
                <DropdownMenuItem
                  onSelect={(e) => {
                    e.preventDefault()
                    setEditOpen(true)
                  }}
                >
                  <Pencil />
                  Edit
                </DropdownMenuItem>
              )}
              {canEdit && <DropdownMenuSeparator />}
              {canEdit && (
                <DropdownMenuItem
                  variant="destructive"
                  onSelect={(e) => {
                    e.preventDefault()
                    setDeleteOpen(true)
                  }}
                >
                  <Trash2 />
                  Delete credential
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        ) : (
          // A managed row that is already the default has nothing to offer.
          // The spacer keeps its left column the same width as every other
          // row's, so the list does not go ragged.
          <span className="inline-block h-7 w-7" aria-hidden />
        )}
      </div>

      {/* Mounted only while open: a resident instance would re-seed its form
          from a background refetch and lose the user's in-progress edit. */}
      {editOpen && (
        <EditAICredentialDialog
          credential={credential}
          open
          onOpenChange={setEditOpen}
          onSaved={() => setAffectedOpen(true)}
        />
      )}

      <DeleteAICredentialDialog
        credential={deleteOpen ? credential : null}
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
      />

      {affectedOpen && (
        <AffectedEnvironmentsDialog
          open
          onOpenChange={setAffectedOpen}
          credentialId={credential.id}
          credentialName={credential.name}
        />
      )}
    </div>
  )
}
