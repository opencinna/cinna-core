import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"
import { UsersService } from "@/client"
import { Button } from "@/components/ui/button"
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
import { LoadingButton } from "@/components/ui/loading-button"
import { PasswordInput } from "@/components/ui/password-input"
import useAuth from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"

const formSchema = z
  .object({
    new_password: z
      .string()
      .min(8, "Password must be at least 8 characters"),
    confirm_password: z.string(),
  })
  .refine((data) => data.new_password === data.confirm_password, {
    message: "Passwords don't match",
    path: ["confirm_password"],
  })

type FormData = z.infer<typeof formSchema>

export default function SetPassword() {
  const [open, setOpen] = useState(false)
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      new_password: "",
      confirm_password: "",
    },
  })

  const mutation = useMutation({
    mutationFn: (data: { new_password: string }) =>
      UsersService.setPasswordMe({ requestBody: data }),
    onSuccess: () => {
      showSuccessToast("Password set successfully")
      form.reset()
      setOpen(false)
      queryClient.invalidateQueries({ queryKey: ["currentUser"] })
    },
    onError: (error: Error) => {
      showErrorToast(error.message || "Failed to set password")
    },
  })

  if (!user || user.has_password) {
    return null
  }

  return (
    <Dialog open={open} onOpenChange={(v) => { setOpen(v); if (!v) form.reset() }}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">Set Password</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Set Password</DialogTitle>
          <DialogDescription>
            You signed up with Google. Set a password to enable password login.
          </DialogDescription>
        </DialogHeader>
        <Form {...form}>
          <form
            onSubmit={form.handleSubmit((data) => mutation.mutate(data))}
            className="flex flex-col gap-4"
          >
            <FormField
              control={form.control}
              name="new_password"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>New Password</FormLabel>
                  <FormControl>
                    <PasswordInput placeholder="••••••••" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="confirm_password"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Confirm Password</FormLabel>
                  <FormControl>
                    <PasswordInput placeholder="••••••••" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <LoadingButton
              type="submit"
              loading={mutation.isPending}
              className="self-start"
            >
              Set Password
            </LoadingButton>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
