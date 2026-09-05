import { zodResolver } from "@hookform/resolvers/zod"
import {
  createFileRoute,
  Link as RouterLink,
  redirect,
} from "@tanstack/react-router"
import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import type { Body_login_login_access_token as AccessToken } from "@/client"
import { GoogleLoginButton } from "@/components/Auth/GoogleLoginButton"
import { AuthLayout } from "@/components/Common/AuthLayout"
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
import { useLocalAgentKitAvailable } from "@/hooks/useLocalAgentKit"
import { APP_NAME, safeRedirectPath } from "@/utils"

const formSchema = z.object({
  username: z.email(),
  password: z
    .string()
    .min(1, { message: "Password is required" })
    .min(8, { message: "Password must be at least 8 characters" }),
}) satisfies z.ZodType<AccessToken>

type FormData = z.infer<typeof formSchema>

const searchSchema = z.object({
  redirect: z.string().optional(),
})

export const Route = createFileRoute("/login/")({
  component: Login,
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
        title: `Log In - ${APP_NAME}`,
      },
    ],
  }),
})

function Login() {
  const { loginMutation } = useAuth()
  const { redirect: redirectParam } = Route.useSearch()
  const signupSearch = redirectParam ? { redirect: redirectParam } : undefined
  const localAgentKitAvailable = useLocalAgentKitAvailable()
  // Fetched, not assumed: a Google-only instance must not flash a password
  // form, so the credential block waits for the answer rather than rendering
  // the permissive default and correcting itself.
  const { data: policy, isPending: policyPending } = useAccessPolicy()
  // A failed policy read leaves both fallbacks permissive. The backend refuses
  // the password grant either way; showing the form is a worse guess than
  // hiding the only way in.
  const passwordAuthEnabled = policy?.password_auth_enabled ?? true
  const registrationOpen = policy?.registration_open ?? true

  // Two independently configured facts, and both have to be true for a Google
  // button to appear: the backend's client id/secret (which the projection
  // reports) and the frontend build's own `VITE_GOOGLE_CLIENT_ID`, which
  // `GoogleLoginButton` returns null without. Reading only one of them is how
  // this page ends up hiding the password form in favour of a button that was
  // never rendered.
  const googleAvailable =
    Boolean(import.meta.env.VITE_GOOGLE_CLIENT_ID) &&
    (policy?.google_auth_enabled ?? true)

  // The break-glass disclosure: administrators keep password sign-in even on a
  // Google-only server, so the form is hidden rather than removed.
  const [passwordFormRevealed, setPasswordFormRevealed] = useState(false)
  // `!googleAvailable` is the floor: a policy that turns off password sign-in
  // on a build that cannot show Google would otherwise leave this page with no
  // way in at all. The backend's lockout rule only checks its own settings, so
  // it cannot catch that combination.
  const showPasswordForm =
    passwordAuthEnabled || passwordFormRevealed || !googleAvailable

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      username: "",
      password: "",
    },
  })

  // Revealing the form unmounts the button that had focus, so move focus into
  // the form rather than letting it fall back to <body> unannounced.
  useEffect(() => {
    if (passwordFormRevealed) {
      form.setFocus("username")
    }
  }, [passwordFormRevealed, form])

  const onSubmit = (data: FormData) => {
    if (loginMutation.isPending) return
    loginMutation.mutate(data)
  }

  return (
    <AuthLayout>
      <Form {...form}>
        <form
          onSubmit={form.handleSubmit(onSubmit)}
          className="flex flex-col gap-6"
        >
          <div className="flex flex-col items-center gap-2 text-center">
            <h1 className="text-2xl font-bold">Login to your account</h1>
          </div>

          {/* Google OAuth Button - ABOVE password form */}
          {googleAvailable && <GoogleLoginButton />}

          {policyPending ? (
            <div className="grid gap-4">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : (
            <>
              {/* Divider - only shown when there is a button above it and a
                  second method below it to divide from */}
              {googleAvailable && showPasswordForm && (
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

              {showPasswordForm ? (
                <div className="grid gap-4">
                  <FormField
                    control={form.control}
                    name="username"
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
                        <FormMessage className="text-xs" />
                      </FormItem>
                    )}
                  />

                  <FormField
                    control={form.control}
                    name="password"
                    render={({ field }) => (
                      <FormItem>
                        <div className="flex items-center">
                          <FormLabel>Password</FormLabel>
                          <RouterLink
                            to="/recover-password"
                            className="ml-auto text-sm underline-offset-4 hover:underline"
                          >
                            Forgot your password?
                          </RouterLink>
                        </div>
                        <FormControl>
                          <PasswordInput
                            data-testid="password-input"
                            placeholder="Password"
                            {...field}
                          />
                        </FormControl>
                        <FormMessage className="text-xs" />
                      </FormItem>
                    )}
                  />

                  <LoadingButton
                    type="submit"
                    loading={loginMutation.isPending}
                  >
                    Log In
                  </LoadingButton>
                </div>
              ) : (
                <div className="text-center">
                  <button
                    type="button"
                    aria-expanded={passwordFormRevealed}
                    onClick={() => setPasswordFormRevealed(true)}
                    className="text-xs text-muted-foreground underline underline-offset-4 hover:text-foreground"
                  >
                    Sign in with password (administrators)
                  </button>
                </div>
              )}
            </>
          )}

          {!policyPending && registrationOpen && (
            <div className="text-center text-sm">
              Don't have an account yet?{" "}
              <RouterLink
                to="/signup"
                search={signupSearch}
                className="underline underline-offset-4"
              >
                Sign up
              </RouterLink>
            </div>
          )}

          {/*
            The link points at the pretty `/agent-start`, not the `/api/agent-start` alias
            the probe used: it is the URL a person would share or type. On an
            instance whose reverse proxy has no `location /agent-start` block that
            lands on the SPA shell instead — see `frontend/nginx.conf` and
            `docs/infrastructure/nginx_setup.md`, which is where that is fixed.
          */}
          {localAgentKitAvailable && (
            <div className="text-center text-xs text-muted-foreground">
              <a
                href="/agent-start?format=html"
                target="_blank"
                rel="noopener"
                className="underline underline-offset-4 hover:text-foreground"
              >
                Building agents locally with Claude Code or Codex? Start here
              </a>
            </div>
          )}
        </form>
      </Form>
    </AuthLayout>
  )
}
