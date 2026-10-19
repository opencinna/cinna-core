import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useEffect } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { AdminUserGroupsService, type UserGroupPublic } from "@/client"
import { ApiError } from "@/client/core/ApiError"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { LoadingButton } from "@/components/ui/loading-button"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { invalidateUserGroup, userGroupKeys } from "./userGroupQueries"

const formSchema = z.object({
  name: z
    .string()
    .trim()
    .min(1, { message: "Name is required" })
    .max(100, { message: "Name must be 100 characters or fewer" }),
  description: z.string().optional(),
})

type FormData = z.infer<typeof formSchema>

interface UserGroupFormDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The group to edit; omit to create one. */
  group?: UserGroupPublic
}

/**
 * S3 — create a group, or edit its name and description: one form, two
 * fields. Creating lands on the new group's Members tab, because adding
 * people is the next step (P6 links onward).
 */
export function UserGroupFormDialog({
  open,
  onOpenChange,
  group,
}: UserGroupFormDialogProps) {
  const isEdit = group !== undefined
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    defaultValues: { name: "", description: "" },
  })

  // Every open starts from the group as it is now (or blank for Create).
  useEffect(() => {
    if (open) {
      form.reset({
        name: group?.name ?? "",
        description: group?.description || "",
      })
    }
  }, [open, group, form])

  const mutation = useMutation({
    mutationFn: (data: FormData) => {
      const requestBody = {
        name: data.name,
        description: data.description?.trim() || null,
      }
      return group
        ? AdminUserGroupsService.updateUserGroup({
            groupId: group.id,
            requestBody,
          })
        : AdminUserGroupsService.createUserGroup({ requestBody })
    },
    onSuccess: (saved) => {
      onOpenChange(false)
      if (isEdit) {
        showSuccessToast("Group updated")
        void invalidateUserGroup(queryClient, saved.id)
        return
      }
      showSuccessToast("Group created")
      queryClient.setQueryData(userGroupKeys.detail(saved.id), saved)
      void queryClient.invalidateQueries({ queryKey: userGroupKeys.list })
      void navigate({
        to: "/admin/user-group/$groupId",
        params: { groupId: saved.id },
        hash: "members",
      })
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        form.setError("name", {
          message: "A group with this name already exists",
        })
        return
      }
      handleError.bind(showErrorToast)(error as ApiError)
    },
  })

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Escape and outside-click must not close a dialog mid-request.
        if (!mutation.isPending) onOpenChange(next)
      }}
    >
      <DialogContent className="sm:max-w-md">
        <Form {...form}>
          <form onSubmit={form.handleSubmit((data) => mutation.mutate(data))}>
            <DialogHeader>
              <DialogTitle>{isEdit ? "Edit group" : "Create group"}</DialogTitle>
              <DialogDescription>
                {isEdit
                  ? "Members and shares stay as they are."
                  : "Name the group, then add the people it stands for."}
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <FormField
                control={form.control}
                name="name"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>
                      Name <span className="text-destructive">*</span>
                    </FormLabel>
                    <FormControl>
                      <Input
                        placeholder="Engineering"
                        maxLength={100}
                        autoFocus
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="description"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Description</FormLabel>
                    <FormControl>
                      <Textarea
                        rows={3}
                        placeholder="What these people have in common"
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={mutation.isPending}
                onClick={() => onOpenChange(false)}
              >
                Cancel
              </Button>
              <LoadingButton type="submit" loading={mutation.isPending}>
                {isEdit ? "Save" : "Create group"}
              </LoadingButton>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
