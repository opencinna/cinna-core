import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Trash2 } from "lucide-react"
import { useState } from "react"
import { useForm } from "react-hook-form"

import { type CredentialPublic, CredentialsService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { DropdownMenuItem } from "@/components/ui/dropdown-menu"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

interface DeleteCredentialProps {
  credential: CredentialPublic
  onSuccess: () => void
  isOpen?: boolean
  setIsOpen?: (open: boolean) => void
  children?: React.ReactNode
}

const DeleteCredential = ({
  credential,
  onSuccess,
  isOpen: controlledIsOpen,
  setIsOpen: controlledSetIsOpen,
  children,
}: DeleteCredentialProps) => {
  const [uncontrolledIsOpen, setUncontrolledIsOpen] = useState(false)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { handleSubmit } = useForm()

  const isOpen = controlledIsOpen ?? uncontrolledIsOpen
  const setIsOpen = controlledSetIsOpen ?? setUncontrolledIsOpen

  const deleteCredential = async (id: string) => {
    await CredentialsService.deleteCredential({ id: id })
  }

  const mutation = useMutation({
    mutationFn: deleteCredential,
    onSuccess: () => {
      showSuccessToast("The credential was deleted successfully")
      setIsOpen(false)
      onSuccess()
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries()
    },
  })

  const onSubmit = async () => {
    mutation.mutate(credential.id)
  }

  return (
    <Dialog open={isOpen} onOpenChange={setIsOpen}>
      {children ? (
        <DialogTrigger asChild>{children}</DialogTrigger>
      ) : (
        <DropdownMenuItem
          variant="destructive"
          onSelect={(e) => e.preventDefault()}
          onClick={() => setIsOpen(true)}
        >
          <Trash2 />
          Delete Credential
        </DropdownMenuItem>
      )}
      <DialogContent className="sm:max-w-md">
        <form onSubmit={handleSubmit(onSubmit)}>
          <DialogHeader>
            <DialogTitle>Delete Credential</DialogTitle>
            <DialogDescription>
              This credential will be permanently deleted. Are you sure? You will not
              be able to undo this action.
            </DialogDescription>
          </DialogHeader>

          <DialogFooter className="mt-4">
            <DialogClose asChild>
              <Button variant="outline" disabled={mutation.isPending}>
                Cancel
              </Button>
            </DialogClose>
            <LoadingButton
              variant="destructive"
              type="submit"
              loading={mutation.isPending}
            >
              Delete
            </LoadingButton>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export default DeleteCredential
