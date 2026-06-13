import { useMutation, useQueryClient } from "@tanstack/react-query"
import { CheckCircle2, EllipsisVertical, Pencil, Trash } from "lucide-react"
import { useState } from "react"

import {
  type AdminAICredentialPublic,
  type AICredentialUpdate,
  AdminLlmProvidersService,
} from "@/client"
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
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { MANAGED_CREDENTIALS_QUERY_PREFIX } from "./providerTypes"

interface LlmProviderActionsMenuProps {
  credential: AdminAICredentialPublic
}

export function LlmProviderActionsMenu({ credential }: LlmProviderActionsMenuProps) {
  const [isEditOpen, setIsEditOpen] = useState(false)
  const [isDeleteOpen, setIsDeleteOpen] = useState(false)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const showBaseUrl =
    credential.type === "openai_compatible" || credential.type === "google"
  const showModel = credential.type === "openai_compatible"

  // Local edit form state (metadata-only: name, api_key, base_url, model).
  const [name, setName] = useState(credential.name)
  const [apiKey, setApiKey] = useState("")
  const [baseUrl, setBaseUrl] = useState(credential.base_url ?? "")
  const [model, setModel] = useState(credential.model ?? "")

  const resetEditForm = () => {
    setName(credential.name)
    setApiKey("")
    setBaseUrl(credential.base_url ?? "")
    setModel(credential.model ?? "")
  }

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX })

  const updateMutation = useMutation({
    mutationFn: (body: AICredentialUpdate) =>
      AdminLlmProvidersService.updateManagedAiCredential({
        credentialId: credential.id,
        requestBody: body,
      }),
    onSuccess: () => {
      showSuccessToast("Credential updated.")
      setIsEditOpen(false)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => void invalidate(),
  })

  const setDefaultMutation = useMutation({
    mutationFn: () =>
      AdminLlmProvidersService.setManagedAiCredentialDefault({
        credentialId: credential.id,
      }),
    onSuccess: () => {
      showSuccessToast("Set as the owner's default credential.")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => void invalidate(),
  })

  const deleteMutation = useMutation({
    mutationFn: () =>
      AdminLlmProvidersService.deleteManagedAiCredential({
        credentialId: credential.id,
      }),
    onSuccess: () => {
      showSuccessToast("Credential deleted.")
      setIsDeleteOpen(false)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => void invalidate(),
  })

  const onSubmitEdit = () => {
    // For OpenAI-compatible credentials base_url and model are mandatory, so
    // block clearing them out (mirrors the provision form rules).
    if (showBaseUrl && credential.type === "openai_compatible" && baseUrl.trim() === "") {
      showErrorToast("Base URL is required for OpenAI Compatible providers.")
      return
    }
    if (showModel && model.trim() === "") {
      showErrorToast("Model is required for OpenAI Compatible providers.")
      return
    }

    const body: AICredentialUpdate = {}
    if (name.trim() !== credential.name) body.name = name.trim()
    if (apiKey.trim() !== "") body.api_key = apiKey
    if (showBaseUrl && baseUrl !== (credential.base_url ?? "")) {
      body.base_url = baseUrl.trim() || null
    }
    if (showModel && model !== (credential.model ?? "")) {
      body.model = model.trim() || null
    }

    if (Object.keys(body).length === 0) {
      showErrorToast("No changes to save.")
      return
    }
    updateMutation.mutate(body)
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="sm" className="h-8 w-8 p-0">
            <EllipsisVertical className="h-4 w-4" />
            <span className="sr-only">Open menu</span>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            onClick={() => {
              resetEditForm()
              setIsEditOpen(true)
            }}
          >
            <Pencil className="mr-2 h-4 w-4" />
            Edit
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() => setDefaultMutation.mutate()}
            disabled={credential.is_default || setDefaultMutation.isPending}
          >
            <CheckCircle2 className="mr-2 h-4 w-4" />
            {credential.is_default ? "Already default" : "Set as default"}
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() => setIsDeleteOpen(true)}
            className="text-destructive focus:text-destructive"
          >
            <Trash className="mr-2 h-4 w-4" />
            Delete
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {/* Edit dialog */}
      <Dialog
        open={isEditOpen}
        onOpenChange={(open) => {
          setIsEditOpen(open)
          if (!open) resetEditForm()
        }}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Edit Credential</DialogTitle>
            <DialogDescription>
              Update the credential on behalf of its owner. Leave the API key blank
              to keep the existing key.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="edit-name">Name</Label>
              <Input
                id="edit-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-api-key">API Key</Label>
              <Input
                id="edit-api-key"
                type="password"
                autoComplete="off"
                placeholder="Leave blank to keep existing key"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
              />
            </div>
            {showBaseUrl && (
              <div className="space-y-2">
                <Label htmlFor="edit-base-url">Base URL</Label>
                <Input
                  id="edit-base-url"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                />
              </div>
            )}
            {showModel && (
              <div className="space-y-2">
                <Label htmlFor="edit-model">Model</Label>
                <Input
                  id="edit-model"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                />
              </div>
            )}
          </div>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline" disabled={updateMutation.isPending}>
                Cancel
              </Button>
            </DialogClose>
            <LoadingButton onClick={onSubmitEdit} loading={updateMutation.isPending}>
              Save
            </LoadingButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete confirm */}
      <AlertDialog open={isDeleteOpen} onOpenChange={setIsDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Credential</AlertDialogTitle>
            <AlertDialogDescription>
              Delete "{credential.name}" provisioned for this user? This removes the
              user's credential. This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                deleteMutation.mutate()
              }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteMutation.isPending ? "Deleting..." : "Delete"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
