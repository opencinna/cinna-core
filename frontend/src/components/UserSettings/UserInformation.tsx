import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, BadgeCheck, Pencil } from "lucide-react"
import { useEffect, useState } from "react"
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

  // ── Email confirmation: resend button + cooldown countdown ──────────
  const emailConfirmed = currentUser?.email_confirmed === true
  const [resendDisabledUntil, setResendDisabledUntil] = useState<number | null>(
    () => {
      const at = currentUser?.confirmation_resend_available_at
      return at ? new Date(at).getTime() : null
    },
  )
  const [now, setNow] = useState(() => Date.now())

  // Restore the cooldown from the server after a page reload — the resend
  // window lives on the user row, so /users/me tells us when the next send
  // is allowed even though the prior mutation response is long gone.
  useEffect(() => {
    const at = currentUser?.confirmation_resend_available_at
    if (at) setResendDisabledUntil(new Date(at).getTime())
  }, [currentUser?.confirmation_resend_available_at])

  // Tick once a second while the resend button is cooling down so the
  // countdown updates and the button re-enables when the window elapses.
  useEffect(() => {
    if (resendDisabledUntil === null) return
    const interval = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(interval)
  }, [resendDisabledUntil])

  const cooldownRemainingMs =
    resendDisabledUntil !== null ? Math.max(0, resendDisabledUntil - now) : 0
  const resendDisabled = cooldownRemainingMs > 0

  const resendMutation = useMutation({
    mutationFn: () => UsersService.resendConfirmationMe(),
    onSuccess: (data) => {
      if (data.sent) {
        showSuccessToast("Confirmation email sent")
      } else {
        showErrorToast(data.message || "No confirmation email was sent")
      }
      if (data.resend_available_at) {
        setResendDisabledUntil(new Date(data.resend_available_at).getTime())
      }
    },
    onError: handleError.bind(showErrorToast),
  })

  const formatCountdown = (ms: number) => {
    const total = Math.ceil(ms / 1000)
    const m = Math.floor(total / 60)
    const s = total % 60
    return `${m}:${s.toString().padStart(2, "0")}`
  }

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
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>User Information</CardTitle>
          <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
            <Pencil className="h-4 w-4 mr-2" />
            Edit
          </Button>
        </div>
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

            <span className="self-start text-sm font-medium text-right">Email</span>
            <div className="flex flex-col gap-2 min-w-0 self-start">
              <div className="flex items-center gap-2 min-w-0">
                <p className="truncate">{currentUser?.email}</p>
                {emailConfirmed ? (
                  <span className="flex items-center gap-1 text-xs text-green-600 shrink-0">
                    <BadgeCheck className="h-4 w-4" aria-hidden="true" />
                    Confirmed
                  </span>
                ) : (
                  <span className="flex items-center gap-1 text-xs text-amber-500 shrink-0">
                    <AlertTriangle className="h-4 w-4" aria-hidden="true" />
                    Not confirmed
                  </span>
                )}
              </div>
              {!emailConfirmed && (
                <div>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-7 text-xs"
                    disabled={resendDisabled || resendMutation.isPending}
                    onClick={() => resendMutation.mutate()}
                  >
                    {resendDisabled
                      ? `Resend available in ${formatCountdown(cooldownRemainingMs)}`
                      : "Resend confirmation"}
                  </Button>
                </div>
              )}
            </div>
          </div>
      </CardContent>
      <Dialog open={open} onOpenChange={(v) => { setOpen(v); if (!v) form.reset() }}>
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

                  <div className="flex justify-end gap-2">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => setOpen(false)}
                      disabled={mutation.isPending}
                    >
                      Cancel
                    </Button>
                    <LoadingButton
                      type="submit"
                      size="sm"
                      loading={mutation.isPending}
                      disabled={!form.formState.isDirty}
                    >
                      Save
                    </LoadingButton>
                  </div>
                </form>
              </Form>
            </DialogContent>
      </Dialog>
    </Card>
  )
}

export default UserInformation
