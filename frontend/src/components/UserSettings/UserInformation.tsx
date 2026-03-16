import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { UsersService, type UserUpdateMe } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
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
import ChangePassword from "@/components/UserSettings/ChangePassword"
import SetPassword from "@/components/UserSettings/SetPassword"
import OAuthAccounts from "@/components/UserSettings/OAuthAccounts"
import useAuth from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import { handleError } from "@/utils"

const formSchema = z.object({
  username: z
    .string()
    .max(50)
    .regex(/^[a-zA-Z0-9_]*$/, { message: "Only letters, numbers, and underscores allowed" })
    .optional()
    .or(z.literal("")),
  full_name: z.string().max(30).optional(),
})

type FormData = z.infer<typeof formSchema>

const UserInformation = () => {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [open, setOpen] = useState(false)
  const { user: currentUser } = useAuth()

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      username: currentUser?.username ?? "",
      full_name: currentUser?.full_name ?? undefined,
    },
  })

  const mutation = useMutation({
    mutationFn: (data: UserUpdateMe) =>
      UsersService.updateUserMe({ requestBody: data }),
    onSuccess: () => {
      showSuccessToast("User updated successfully")
      setOpen(false)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries()
    },
  })

  const onSubmit = (data: FormData) => {
    const updateData: UserUpdateMe = {}

    if (data.username !== currentUser?.username) {
      updateData.username = data.username || null
    }
    if (data.full_name !== currentUser?.full_name) {
      updateData.full_name = data.full_name
    }

    mutation.mutate(updateData)
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <Card>
        <CardHeader>
          <CardTitle>User Information</CardTitle>
          <CardDescription>Manage your personal details</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-[100px_1fr] items-center gap-x-3 gap-y-4">
            <span className="text-sm font-medium text-right">Username</span>
            <p className={cn("truncate", !currentUser?.username && "text-muted-foreground")}>
              {currentUser?.username || "Not set"}
            </p>

            <span className="text-sm font-medium text-right">Full name</span>
            <p className={cn("truncate", !currentUser?.full_name && "text-muted-foreground")}>
              {currentUser?.full_name || "N/A"}
            </p>

            <span className="text-sm font-medium text-right">Email</span>
            <p className="truncate">{currentUser?.email}</p>
          </div>

          <div className="flex gap-3">
            <Dialog open={open} onOpenChange={(v) => { setOpen(v); if (!v) form.reset() }}>
              <DialogTrigger asChild>
                <Button variant="outline" size="sm">Edit Profile</Button>
              </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Edit Profile</DialogTitle>
                <DialogDescription>Update your personal information.</DialogDescription>
              </DialogHeader>
              <Form {...form}>
                <form
                  onSubmit={form.handleSubmit(onSubmit)}
                  className="flex flex-col gap-4"
                >
                  <FormField
                    control={form.control}
                    name="username"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>Username</FormLabel>
                        <FormControl>
                          <Input type="text" placeholder="my_username" {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />

                  <FormField
                    control={form.control}
                    name="full_name"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>Full name</FormLabel>
                        <FormControl>
                          <Input type="text" {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />

                  <LoadingButton
                    type="submit"
                    loading={mutation.isPending}
                    disabled={!form.formState.isDirty}
                    className="self-start"
                  >
                    Save
                  </LoadingButton>
                </form>
              </Form>
            </DialogContent>
            </Dialog>
            {currentUser?.has_password ? <ChangePassword /> : <SetPassword />}
          </div>
        </CardContent>
      </Card>

      <OAuthAccounts />
    </div>
  )
}

export default UserInformation
