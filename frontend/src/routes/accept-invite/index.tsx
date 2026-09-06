import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery } from "@tanstack/react-query"
import {
  createFileRoute,
  Link as RouterLink,
  useNavigate,
} from "@tanstack/react-router"
import { useMemo, useState } from "react"
import { useForm } from "react-hook-form"
import { FcGoogle } from "react-icons/fc"
import { z } from "zod"

import { InvitationsService } from "@/client"
import { AuthLayout } from "@/components/Common/AuthLayout"
import { Button } from "@/components/ui/button"
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
import { googleSignInAvailable } from "@/hooks/useAccessPolicy"
import { isMfaChallengeResponse } from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import {
  APP_NAME,
  clearLoginScopedDisclaimerAck,
  getErrorMessage,
  persistDetectedLocaleDefaults,
} from "@/utils"

const searchSchema = z.object({
  token: z.string().catch(""),
})

const formSchema = z
  .object({
    full_name: z.string().optional(),
    password: z
      .string()
      .min(1, { message: "Password is required" })
      .min(8, { message: "Password must be at least 8 characters" })
      .max(128, { message: "Password must be at most 128 characters" }),
    confirm_password: z
      .string()
      .min(1, { message: "Password confirmation is required" }),
  })
  .refine((data) => data.password === data.confirm_password, {
    message: "The passwords don't match",
    path: ["confirm_password"],
  })

type FormData = z.infer<typeof formSchema>

export const Route = createFileRoute("/accept-invite/")({
  component: AcceptInvite,
  validateSearch: searchSchema,
  head: () => ({
    meta: [{ title: `Accept Invitation - ${APP_NAME}` }],
  }),
})

/**
 * The only thing an unusable invitation is ever told.
 *
 * The backend makes every failure identical on purpose — a forged token, a
 * revoked invitation, an expired one, an address that was changed and one
 * that never existed all answer `{"valid": false}` byte for byte. Adding a
 * reason here would rebuild, in the UI, exactly the enumeration oracle the
 * endpoint was designed to close.
 */
function InvalidInvitation() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col items-center gap-2 text-center">
        <h1 className="text-2xl font-bold">
          This invitation is no longer valid
        </h1>
      </div>
      <div className="text-center text-sm">
        <RouterLink to="/login" className="underline underline-offset-4">
          Go to sign in
        </RouterLink>
      </div>
    </div>
  )
}

function AcceptInvite() {
  const { token } = Route.useSearch()
  const navigate = useNavigate()
  const { showErrorToast } = useCustomToast()

  // Set once the accept endpoint has refused the token. Every such refusal is
  // terminal and detail-free, so the page becomes the invalid panel rather
  // than leaving a form on screen that can only fail again.
  const [refused, setRefused] = useState(false)

  const {
    data: lookup,
    isPending,
    isError,
  } = useQuery({
    queryKey: ["invitation", token],
    queryFn: () =>
      InvitationsService.lookupInvitation({ requestBody: { token } }),
    // A lookup failure is not something a retry fixes, and repeating it turns
    // one page load into several hits on a rate-limited endpoint.
    retry: false,
    enabled: token.length > 0,
  })

  const prefill = useMemo(
    () =>
      lookup?.valid
        ? {
            full_name: lookup.full_name ?? "",
            password: "",
            confirm_password: "",
          }
        : undefined,
    [lookup],
  )

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      full_name: "",
      password: "",
      confirm_password: "",
    },
    // Memoised so the reference is stable: react-hook-form re-runs its
    // `values` effect on every render, and a fresh object each time would keep
    // handing it new "server" values while the person is typing.
    values: prefill,
  })

  const includeDesktop = lookup?.include_desktop === true

  const mutation = useMutation({
    mutationFn: (data: FormData) =>
      InvitationsService.acceptInvitation({
        requestBody: {
          token,
          password: data.password,
          full_name: data.full_name?.trim() ? data.full_name.trim() : null,
        },
      }),
    onSuccess: (result) => {
      if (isMfaChallengeResponse(result)) {
        // Unreachable by construction — the account is being given its first
        // password in this very request, so it cannot have enrolled a second
        // factor. Handled rather than cast away so a future change that makes
        // it reachable lands the person on a working page.
        void navigate({ to: "/login" })
        return
      }
      localStorage.setItem("access_token", result.access_token)
      clearLoginScopedDisclaimerAck()
      // Fire-and-forget, as on every other sign-in path.
      persistDetectedLocaleDefaults()
      // A full assign rather than a client-side navigate, matching
      // `useAuth.loginMutation` and `GoogleLoginButton`: the identity just
      // changed, and no React Query cache belonging to the previous one
      // should survive it. `?desktop=true` parses back to a boolean
      // identically to the object form.
      window.location.assign(`/accept-invite/done?desktop=${includeDesktop}`)
    },
    onError: (error) => {
      const status = (error as { status?: number })?.status
      if (status === 400) {
        setRefused(true)
        return
      }
      showErrorToast(getErrorMessage(error, "Could not accept the invitation."))
    },
  })

  if (!token || isError || refused || (lookup && !lookup.valid)) {
    return (
      <AuthLayout>
        <InvalidInvitation />
      </AuthLayout>
    )
  }

  if (isPending || !lookup) {
    return (
      <AuthLayout>
        <div className="flex flex-col gap-4">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      </AuthLayout>
    )
  }

  // The one and only question about passwords, answered server-side by
  // `AccessPolicyService.is_password_auth_allowed` and delivered as a single
  // derived boolean. `password_accepted` is *absent* on an invalid token, so
  // an explicit `=== true` is what distinguishes "allowed" from both "not
  // allowed" and "not told" — and this page never recombines it with
  // `auth_hint` or with the public access policy. Two implementations of one
  // policy question is precisely the drift this shape exists to prevent.
  const passwordAccepted = lookup.password_accepted === true
  // Two independently configured facts — the backend's Google client id and
  // secret (which the lookup reports) and this frontend build's own
  // `VITE_GOOGLE_CLIENT_ID`, without which `GoogleLoginButton` renders
  // nothing. Reading only the first is how this page offers "Continue with
  // Google" and hands the invitee a login screen with no Google button and an
  // account that has no password.
  //
  // Resolved by the one shared helper, the same one `/login`, `/signup` and
  // `/start` use — the fact arrives on a different projection here, which is
  // why the helper is structurally typed. `=== true` because a lookup for an
  // invalid token omits the field entirely.
  const googleAvailable = googleSignInAvailable(lookup) === true

  // `auth_hint` is presentational: it decides which method is presented first
  // and nothing else. It never hides an available method and never enables an
  // unavailable one.
  const googleLeads = googleAvailable && lookup.auth_hint === "google"

  const googleRedirect = `/accept-invite/done?desktop=${includeDesktop}`

  const googleSection = googleAvailable ? (
    <Button variant="outline" className="w-full" asChild>
      <RouterLink to="/login" search={{ redirect: googleRedirect }}>
        <FcGoogle className="mr-2 size-5" />
        Continue with Google
      </RouterLink>
    </Button>
  ) : null

  const passwordSection = passwordAccepted ? (
    <Form {...form}>
      <form
        onSubmit={form.handleSubmit((data) => mutation.mutate(data))}
        className="grid gap-4"
      >
        <FormField
          control={form.control}
          name="full_name"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Your name</FormLabel>
              <FormControl>
                <Input placeholder="Full name" type="text" {...field} />
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
                <PasswordInput placeholder="Password" {...field} />
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
              <FormLabel>Confirm password</FormLabel>
              <FormControl>
                <PasswordInput placeholder="Confirm password" {...field} />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />

        <LoadingButton
          type="submit"
          className="w-full"
          loading={mutation.isPending}
        >
          Set password and continue
        </LoadingButton>
      </form>
    </Form>
  ) : null

  const divider =
    googleSection && passwordSection ? (
      <div className="relative text-center text-sm">
        <span className="relative z-10 bg-background px-2 text-muted-foreground">
          or
        </span>
        <div className="absolute inset-0 top-1/2 border-t" />
      </div>
    ) : null

  return (
    <AuthLayout>
      <div className="flex flex-col gap-6">
        <div className="flex flex-col items-center gap-2 text-center">
          <h1 className="text-2xl font-bold">
            You've been invited to {lookup.project_name ?? APP_NAME}
          </h1>
          {lookup.email_masked && (
            <p className="text-sm text-muted-foreground">
              This invitation is for {lookup.email_masked}.
            </p>
          )}
        </div>

        {googleLeads ? (
          <>
            {googleSection}
            {divider}
            {passwordSection}
          </>
        ) : (
          <>
            {passwordSection}
            {divider}
            {googleSection}
          </>
        )}

        {!googleSection && !passwordSection && (
          <p className="text-center text-sm text-muted-foreground">
            No sign-in method is available for this account right now. Ask
            whoever invited you to get in touch.
          </p>
        )}

        <div className="text-center text-sm">
          Already have an account?{" "}
          <RouterLink to="/login" className="underline underline-offset-4">
            Sign in
          </RouterLink>
        </div>
      </div>
    </AuthLayout>
  )
}
