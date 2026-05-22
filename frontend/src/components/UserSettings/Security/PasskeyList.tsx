import { Check, Pencil, Trash2, X } from "lucide-react"
import { useState } from "react"

import type { MfaStatus, UserPasskeyPublic } from "@/client"
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
import { Input } from "@/components/ui/input"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import {
  useDeletePasskeyMutation,
  useRenamePasskeyMutation,
} from "@/hooks/useMfa"
import { handleError } from "@/utils"

interface PasskeyListProps {
  passkeys: UserPasskeyPublic[]
  isLoading: boolean
  mfaStatus: MfaStatus | undefined
}

const formatDate = (iso: string | null) => {
  if (!iso) return "never"
  try {
    return new Date(iso).toLocaleDateString()
  } catch {
    return iso
  }
}

/**
 * Renders the list of registered passkeys with inline rename + delete.
 * Empty state is handled by the parent (`PasskeySection`).
 */
export function PasskeyList({
  passkeys,
  isLoading,
  mfaStatus,
}: PasskeyListProps) {
  const { showErrorToast, showSuccessToast } = useCustomToast()
  const rename = useRenamePasskeyMutation()
  const remove = useDeletePasskeyMutation()
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editValue, setEditValue] = useState("")
  const [deleteId, setDeleteId] = useState<string | null>(null)

  // Last factor = 2FA is on, exactly one passkey remains, no TOTP fallback.
  // Removing that passkey turns 2FA off automatically (server-side).
  const isLastFactor =
    !!mfaStatus?.enabled && !mfaStatus.has_totp && passkeys.length === 1

  const handleStartEdit = (passkey: UserPasskeyPublic) => {
    setEditingId(passkey.id)
    setEditValue(passkey.nickname)
  }

  const handleConfirmEdit = (passkey: UserPasskeyPublic) => {
    const trimmed = editValue.trim()
    if (trimmed.length === 0 || trimmed.length > 64) {
      showErrorToast("Nickname must be 1-64 characters")
      return
    }
    if (trimmed === passkey.nickname) {
      setEditingId(null)
      return
    }
    rename.mutate(
      { passkeyId: passkey.id, nickname: trimmed },
      {
        onSuccess: () => {
          showSuccessToast("Passkey renamed")
          setEditingId(null)
        },
        onError: (err) => handleError.call(showErrorToast, err as never),
      },
    )
  }

  const handleConfirmDelete = () => {
    if (!deleteId) return
    remove.mutate(deleteId, {
      onSuccess: () => {
        showSuccessToast(
          isLastFactor
            ? "Passkey removed — two-factor authentication is now off"
            : "Passkey removed",
        )
        setDeleteId(null)
      },
      onError: (err) => {
        handleError.call(showErrorToast, err as never)
        setDeleteId(null)
      },
    })
  }

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading passkeys...</p>
  }

  if (passkeys.length === 0) {
    return null
  }

  return (
    <>
      <div className="space-y-1.5">
        {passkeys.map((passkey) => (
          <div
            key={passkey.id}
            className="flex items-center justify-between px-3 py-2 border rounded-lg"
          >
            <div className="flex items-center gap-2 min-w-0 flex-1">
              {editingId === passkey.id ? (
                <Input
                  value={editValue}
                  onChange={(e) => setEditValue(e.target.value)}
                  maxLength={64}
                  autoFocus
                  className="h-7 text-sm"
                />
              ) : (
                <>
                  <span className="font-medium text-sm truncate">
                    {passkey.nickname}
                  </span>
                  <Badge variant="outline" className="text-xs shrink-0">
                    {passkey.device_type === "platform"
                      ? "Platform"
                      : "Cross-platform"}
                  </Badge>
                  {passkey.backed_up && (
                    <Badge variant="secondary" className="text-xs shrink-0">
                      Synced
                    </Badge>
                  )}
                  <span className="text-xs text-muted-foreground truncate">
                    Added {formatDate(passkey.created_at)} · Last used{" "}
                    {formatDate(passkey.last_used_at)}
                  </span>
                </>
              )}
            </div>
            <div className="flex items-center gap-0.5 shrink-0 ml-2">
              {editingId === passkey.id ? (
                <>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          onClick={() => handleConfirmEdit(passkey)}
                          disabled={rename.isPending}
                        >
                          <Check className="h-3.5 w-3.5" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>Save</TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={() => setEditingId(null)}
                  >
                    <X className="h-3.5 w-3.5" />
                  </Button>
                </>
              ) : (
                <>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          onClick={() => handleStartEdit(passkey)}
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>Rename</TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          onClick={() => setDeleteId(passkey.id)}
                        >
                          <Trash2 className="h-3.5 w-3.5 text-destructive" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>Remove</TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </>
              )}
            </div>
          </div>
        ))}
      </div>

      <AlertDialog
        open={deleteId !== null}
        onOpenChange={(o) => !o && setDeleteId(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {isLastFactor
                ? "Remove this passkey and turn off 2FA?"
                : "Remove this passkey?"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {isLastFactor
                ? "This is your last 2FA factor. If you remove it, two-factor authentication will be turned off for your account."
                : "You won't be able to sign in with this device anymore. Your other factors stay enrolled."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={remove.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                handleConfirmDelete()
              }}
              disabled={remove.isPending}
              className="bg-destructive text-white hover:bg-destructive/90"
            >
              {isLastFactor ? "Remove and turn off 2FA" : "Remove"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
