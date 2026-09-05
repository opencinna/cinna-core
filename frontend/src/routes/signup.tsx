import { zodResolver } from "@hookform/resolvers/zod"
import {
  createFileRoute,
  Link as RouterLink,
  redirect,
} from "@tanstack/react-router"
import { useForm } from "react-hook-form"
import { z } from "zod"
import { GoogleLoginButton } from "@/components/Auth/GoogleLoginButton"
import { AuthLayout } from "@/components/Common/AuthLayout"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
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
import { PasswordInput } from "@/components/ui/password-input"
import { Skeleton } from "@/components/ui/skeleton"
import { useAccessPolicy } from "@/hooks/useAccessPolicy"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import { APP_NAME, safeRedirectPath } from "@/utils"

const formSchema = z
  .object({
    email: z.email(),
    full_name: z.string().min(1, { message: "Full Name is required" }),
    password: z
      .string()
      .min(1, { message: "Password is required" })
      .min(8, { message: "Password must be at least 8 characters" }),
    confirm_password: z
      .string()
      .min(1, { message: "Password confirmation is required" }),
  })
  .refine((data) => data.password === data.confirm_password, {
    message: "The passwords don't match",
    path: ["confirm_password"],
  })

type FormData = z.infer<typeof formSchema>

const searchSchema = z.object({
  redirect: z.string().optional(),
})

export const Route = createFileRoute("/signup")({
  component: SignUp,
  validateSearch: searchSchema,
  beforeLoad: async ({ search }) => {
    if (isLoggedIn()) {
      const target = safeRedirectPath(search.redirect)
      if (target !== "/") {
        throw redirect({ href: target })
      }
      throw redirect({ to: "/" })
    }
  },
  head: () => ({
    meta: [
      {
        title: `Sign Up - ${APP_NAME}`,
      },
    ],
  }),
})

function SignUp() {
  const { signUpMutation } = useAuth()
  const { redirect: redirectParam } = Route.useSearch()
  const loginSearch = redirectParam ? { redirect: redirectParam } : undefined
  const { data: policy, isPending: policyPending } = useAccessPolicy()

  // Permissive fallbacks when the policy read fails: the signup endpoint
  // refuses with the same reason codes either way, so a wrong guess here costs
  // one rejected request, not an unreachable server.
  const registrationOpen = policy?.registration_open ?? true
  const passwordAuthEnabled = policy?.password_auth_enabled ?? true
  const selfServeSignupAllowed = registrationOpen && passwordAuthEnabled

  // One gate for the one button, in both branches of this file. The backend's
  // client id/secret (reported by the projection) and the frontend build's own
  // `VITE_GOOGLE_CLIENT_ID` are configured independently, and
  // `GoogleLoginButton` renders nothing without the latter — so a divider or a
  // sentence keyed to only one of them can point at a button that is not there.
  const googleAvailable =
    Boolean(import.meta.env.VITE_GOOGLE_CLIENT_ID) &&
    (policy?.google_auth_enabled ?? true)

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      email: "",
      full_name: "",
      password: "",
      confirm_password: "",
    },
  })

  const onSubmit = (data: FormData) => {
    if (signUpMutation.isPending) return

    // exclude confirm_password from submission data
    const { confirm_password: _confirm_password, ...submitData } = data
    signUpMutation.mutate(submitData)
  }

  // The copy must not point at a button that is not there: an invite-only
  // server with no Google OAuth configured is a perfectly legal state, and the
  // backend's lockout rule only guards the password-auth-off case.
  const panelExplanation = registrationOpen
    ? googleAvailable
      ? "Password sign-up is turned off on this server. Continue with Google below, or ask your administrator for access."
      : "Password sign-up is turned off on this server. Ask your administrator for access."
    : googleAvailable
      ? "Ask your administrator for an invitation. If you already have an account, sign in with Google below."
      : "Ask your administrator for an invitation. If you already have an account, sign in from the login page."

  const loginLink = (
    <div className="text-center text-sm">
      Already have an account?{" "}
      <RouterLink
        to="/login"
        search={loginSearch}
        className="underline underline-offset-4"
      >
        Log in
      </RouterLink>
    </div>
  )

  if (policyPending) {
    return (
      <AuthLayout>
        <div className="flex flex-col gap-6">
          <Skeleton className="h-8 w-2/3 self-center" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      </AuthLayout>
    )
  }

  if (!selfServeSignupAllowed) {
    return (
      <AuthLayout>
        <div className="flex flex-col gap-6">
          <div className="flex flex-col items-center gap-2 text-center">
            <h1 className="text-2xl font-bold">Create an account</h1>
          </div>

          <Alert>
            <AlertTitle>
              {registrationOpen
                ? "Accounts are created with Google here"
                : "This server is invite-only"}
            </AlertTitle>
            <AlertDescription>{panelExplanation}</AlertDescription>
          </Alert>

          {/*
            Shown whenever Google sign-in is configured, not only when
            auto-registration is on: in invite-only mode the button is how an
            invited, pre-created account gets in, which is exactly what the
            panel above tells the reader to do. It renders nothing when the
            deployment has no Google client id.
          */}
          {googleAvailable && <GoogleLoginButton />}

          {loginLink}
        </div>
      </AuthLayout>
    )
  }

  return (
    <AuthLayout>
      <Form {...form}>
        <form
          onSubmit={form.handleSubmit(onSubmit)}
          className="flex flex-col gap-6"
        >
          <div className="flex flex-col items-center gap-2 text-center">
            <h1 className="text-2xl font-bold">Create an account</h1>
          </div>

          {/* Google OAuth Button - ABOVE password form */}
          {googleAvailable && <GoogleLoginButton />}

          {/* Divider - only shown when there is a button above it */}
          {googleAvailable && (
            <div className="relative">
              <div className="absolute inset-0 flex items-center">
                <span className="w-full border-t" />
              </div>
              <div className="relative flex justify-center text-xs uppercase">
                <span className="bg-background px-2 text-muted-foreground">
                  Or continue with email
                </span>
              </div>
            </div>
          )}

          <div className="grid gap-4">
            <FormField
              control={form.control}
              name="full_name"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Full Name</FormLabel>
                  <FormControl>
                    <Input
                      data-testid="full-name-input"
                      placeholder="User"
                      type="text"
                      {...field}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="email"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Email</FormLabel>
                  <FormControl>
                    <Input
                      data-testid="email-input"
                      placeholder="user@example.com"
                      type="email"
                      {...field}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="password"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Password</FormLabel>
                  <FormControl>
                    <PasswordInput
                      data-testid="password-input"
                      placeholder="Password"
                      {...field}
                    />
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
                    <PasswordInput
                      data-testid="confirm-password-input"
                      placeholder="Confirm Password"
                      {...field}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <LoadingButton
              type="submit"
              className="w-full"
              loading={signUpMutation.isPending}
            >
              Sign Up
            </LoadingButton>
          </div>

          {loginLink}
        </form>
      </Form>
    </AuthLayout>
  )
}

export default SignUp
