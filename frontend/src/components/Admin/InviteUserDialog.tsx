import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { UserPlus } from "lucide-react"
import { useMemo, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  AdminLlmProvidersService,
  type InviteUserRequest,
  type InviteUserResponse,
  type ManagedAICredentialPublic,
  ServerConfigService,
  UsersService,
} from "@/client"
import { InviteSuccessPanel } from "@/components/Admin/InviteSuccessPanel"
import {
  MANAGED_CREDENTIALS_QUERY_PREFIX,
  managedCredentialsQueryKey,
} from "@/components/Admin/LlmProviders/providerTypes"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
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
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage } from "@/utils"
import {
  ROLE_ADMIN,
  ROLE_AGENT_DEVELOPER,
  ROLE_AGENT_USER,
  USER_ROLE_OPTIONS,
  userRoleLabel,
} from "@/utils/userRoles"

/**
 * The sign-in method the invitation *leads* with.
 *
 * Presentational only, all the way down: the accept page uses it to decide
 * which method is offered first and the email to decide its wording. It never
 * gates — whether a password may be set is answered once, server-side, by
 * `password_accepted` on the lookup, and Google is offered whenever the
 * instance has it. An admin picking "Password" cannot enable password auth on
 * a Google-only instance, and picking "Google" cannot take a password away
 * from someone entitled to one.
 */
const AUTH_HINT_OPTIONS = [
  {
    value: "any",
    label: "Any available method",
    description: "Offer whatever this instance supports.",
  },
  {
    value: "password",
    label: "Password",
    description: "Lead with setting a password.",
  },
  {
    value: "google",
    label: "Google",
    description: "Lead with Sign in with Google.",
  },
] as const

const formSchema = z.object({
  email: z.email({ message: "Invalid email address" }),
  full_name: z.string().optional(),
  role: z.enum([ROLE_AGENT_USER, ROLE_AGENT_DEVELOPER, ROLE_ADMIN]),
  auth_hint: z.enum(["any", "password", "google"]),
  send_email: z.boolean(),
})

type FormData = z.infer<typeof formSchema>

type Step = "who" | "provisioning"

/**
 * Which managed credentials a role would have been auto-provisioned anyway.
 *
 * Derived from `auto_provision_roles` — the *same* field
 * `AccountProvisioningService._provision` filters on — so the wizard's
 * pre-ticked set and an account arriving through Google or signup end up with
 * the same keys. Re-deriving this from anything else is how an invited
 * `agent-user` and a self-registered one silently diverge.
 */
function defaultCredentialIds(
  records: ManagedAICredentialPublic[] | undefined,
  role: string,
): string[] {
  return (records ?? [])
    .filter((record) => (record.auto_provision_roles ?? []).includes(role))
    .map((record) => record.id)
}

const InviteUserDialog = () => {
  const [isOpen, setIsOpen] = useState(false)
  const [step, setStep] = useState<Step>("who")
  const [result, setResult] = useState<InviteUserResponse | null>(null)
  const [duplicateEmail, setDuplicateEmail] = useState(false)
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()

  // `null` means "not stated" and is sent as such: the backend then runs the
  // same auto-provision predicate every other arrival path runs. An empty
  // array means the admin deliberately unticked everything. Keeping the two
  // apart matters when the credential list never loaded — see `onSubmit`.
  const [credentialIds, setCredentialIds] = useState<string[] | null>(null)
  // `null` means "follow the instance default". Held rather than resolved on
  // entering step 2, because an admin who opens the dialog and clicks Next
  // before the server config lands would otherwise be shown `true` on an
  // instance whose admin toggle says false.
  const [includeDesktopChoice, setIncludeDesktopChoice] = useState<
    boolean | null
  >(null)

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      email: "",
      full_name: "",
      role: ROLE_AGENT_USER,
      auth_hint: "any",
      send_email: true,
    },
  })

  const role = form.watch("role")

  // Same key the AI Credentials page and the auto-provision matrix use, so the
  // wizard reads whatever those surfaces last wrote.
  const {
    data: credentials,
    isPending: credentialsPending,
    isError: credentialsError,
  } = useQuery({
    queryKey: managedCredentialsQueryKey(),
    queryFn: () => AdminLlmProvidersService.listManagedAiCredentials({}),
    staleTime: 30_000,
    enabled: isOpen,
  })

  const { data: serverConfig } = useQuery({
    queryKey: ["serverConfig"],
    queryFn: () => ServerConfigService.getServerConfig(),
    enabled: isOpen,
  })

  // What the checkbox *renders*. A checkbox needs a concrete boolean, so this
  // falls back all the way to `true` — but it is a display value only. What
  // gets submitted is decided in `onSubmit`, which sends `null` rather than
  // this fallback when the server config has not resolved; see the comment
  // there.
  const includeDesktop =
    includeDesktopChoice ?? serverConfig?.invite_include_desktop_default ?? true

  const suggestedIds = useMemo(
    () => defaultCredentialIds(credentials, role),
    [credentials, role],
  )

  const mutation = useMutation({
    mutationFn: (data: InviteUserRequest) =>
      UsersService.inviteUser({ requestBody: data }),
    onSuccess: (response) => {
      setResult(response)
    },
    onError: (error) => {
      const message = getErrorMessage(error, "Could not send the invitation.")
      // The duplicate-email refusal is the one worth a targeted hint: the
      // admin's next move is a resend from the row menu, not a second invite.
      // Matched on the message `create_account` raises, which is also what the
      // create-user form keys off.
      setDuplicateEmail(message.includes("already exists in the system"))
      showErrorToast(message)
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["users"] })
      // A grant changes each managed credential's member list.
      queryClient.invalidateQueries({
        queryKey: MANAGED_CREDENTIALS_QUERY_PREFIX,
      })
    },
  })

  const resetAll = () => {
    form.reset()
    setStep("who")
    setResult(null)
    setDuplicateEmail(false)
    setCredentialIds(null)
    setIncludeDesktopChoice(null)
  }

  const handleOpenChange = (open: boolean) => {
    setIsOpen(open)
    if (!open) resetAll()
  }

  const goToProvisioning = async () => {
    const valid = await form.trigger(["email", "full_name", "role"])
    if (!valid) return
    setDuplicateEmail(false)
    // Seed the tick marks from the role's auto-provision set the moment the
    // step opens. Only meaningful once the list has loaded; without it the
    // selection stays `null` and the backend decides, which is the same answer.
    setCredentialIds(credentials ? suggestedIds : null)
    setStep("provisioning")
  }

  const toggleCredential = (id: string, checked: boolean) => {
    setCredentialIds((current) => {
      const base = current ?? suggestedIds
      return checked
        ? Array.from(new Set([...base, id]))
        : base.filter((entry) => entry !== id)
    })
  }

  const onSubmit = (data: FormData) => {
    // Step 1's inputs live in this same <form>, so a stray Enter there would
    // otherwise send the invitation before the admin has seen provisioning.
    if (step !== "provisioning") return
    mutation.mutate({
      email: data.email,
      // Sent as typed. "A blank name is an omission" is a server rule now —
      // `InviteUserRequest` normalises blank and whitespace-only names to
      // `null` at the edge, and `_resume_interrupted` writes a field only
      // when it is not `null`. Trimming and blank-checking here as well would
      // be a second implementation of that rule, and the copy that drifts is
      // always the one nothing enforces.
      full_name: data.full_name ?? null,
      role: data.role,
      auth_hint: data.auth_hint,
      send_email: data.send_email,
      // Same rule as `managed_credential_ids` below, and for the same reason.
      // `null` is "not stated", and the server answers it with
      // `ServerConfig.invite_include_desktop_default` — the one place that
      // policy is stored. Sending the render-time fallback instead would make
      // the client the second implementation of it: on a slow first open, or
      // when this query has failed outright, the wizard would send `true` to
      // an instance whose stored default is `false`.
      include_desktop: serverConfig ? includeDesktop : null,
      // Send the explicit list only when the admin actually saw one. If the
      // credential query failed or is still in flight, "not stated" is the
      // honest answer and lets the server apply its own predicate — sending
      // `[]` there would silently grant nothing.
      managed_credential_ids: credentials
        ? (credentialIds ?? suggestedIds)
        : null,
    })
  }

  return (
    <Dialog open={isOpen} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button className="my-4">
          <UserPlus className="mr-2" />
          Invite user
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        {result ? (
          <InviteSuccessPanel
            result={result}
            onDone={() => handleOpenChange(false)}
            onInviteAnother={resetAll}
          />
        ) : (
          <Form {...form}>
            {/* One <form> across both steps, so react-hook-form keeps step 1's
                values while step 2 is on screen. Enter therefore has to mean
                different things per step: "Next" while the step-1 fields are
                focused, "Send" only once the admin has seen provisioning. */}
            <form
              onSubmit={form.handleSubmit(
                step === "who" ? goToProvisioning : onSubmit,
              )}
            >
              <DialogHeader>
                {/* The stepper the pattern asks for and this dialog never had:
                    two hidden panes give no sense of how far along the admin
                    is, or that there is a second step at all. */}
                <p className="text-xs text-muted-foreground">
                  <span
                    className={
                      step === "who" ? "font-medium text-foreground" : undefined
                    }
                  >
                    1 Who
                  </span>
                  {" · "}
                  <span
                    className={
                      step === "provisioning"
                        ? "font-medium text-foreground"
                        : undefined
                    }
                  >
                    2 Provisioning
                  </span>
                </p>
                <DialogTitle>
                  {step === "who" ? "Invite user" : "Provisioning"}
                </DialogTitle>
                <DialogDescription>
                  {step === "who"
                    ? "Create the account and send its owner a link to claim it. No password is set here — they choose how to sign in."
                    : `What ${form.getValues("email") || "the new account"} starts with.`}
                </DialogDescription>
              </DialogHeader>

              <fieldset
                className="grid min-w-0 gap-4 py-4"
                hidden={step !== "who"}
                disabled={mutation.isPending}
              >
                <FormField
                  control={form.control}
                  name="email"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>
                        Email <span className="text-destructive">*</span>
                      </FormLabel>
                      <FormControl>
                        <Input
                          placeholder="person@example.com"
                          type="email"
                          autoComplete="off"
                          {...field}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />

                {duplicateEmail && (
                  <p className="text-sm text-muted-foreground">
                    That address already has an account. To send its owner a
                    fresh link, use <strong>Resend invitation</strong> from the
                    row menu on the users list.
                  </p>
                )}

                <FormField
                  control={form.control}
                  name="full_name"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Full name</FormLabel>
                      <FormControl>
                        <Input placeholder="Full name" type="text" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />

                <FormField
                  control={form.control}
                  name="role"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Role</FormLabel>
                      <Select
                        onValueChange={field.onChange}
                        value={field.value}
                      >
                        <FormControl>
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                        </FormControl>
                        <SelectContent>
                          {USER_ROLE_OPTIONS.map((option) => (
                            <SelectItem key={option.value} value={option.value}>
                              {option.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <FormMessage />
                    </FormItem>
                  )}
                />

                <FormField
                  control={form.control}
                  name="auth_hint"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Sign-in method</FormLabel>
                      <FormControl>
                        <RadioGroup
                          value={field.value}
                          onValueChange={field.onChange}
                          className="gap-2"
                        >
                          {AUTH_HINT_OPTIONS.map((option) => (
                            <div
                              key={option.value}
                              className="flex items-start gap-2"
                            >
                              <RadioGroupItem
                                id={`auth-hint-${option.value}`}
                                value={option.value}
                                className="mt-0.5"
                              />
                              <div className="min-w-0 flex-1">
                                <Label
                                  htmlFor={`auth-hint-${option.value}`}
                                  className="cursor-pointer font-normal"
                                >
                                  {option.label}
                                </Label>
                                <p className="text-xs text-muted-foreground">
                                  {option.description}
                                </p>
                              </div>
                            </div>
                          ))}
                        </RadioGroup>
                      </FormControl>
                      <p className="text-xs text-muted-foreground">
                        A preference, not a restriction — it decides what the
                        invitation leads with. This instance still decides which
                        methods are available.
                      </p>
                      <FormMessage />
                    </FormItem>
                  )}
                />

                <FormField
                  control={form.control}
                  name="send_email"
                  render={({ field }) => (
                    <FormItem className="flex items-center justify-between gap-4 space-y-0">
                      <div className="min-w-0">
                        <FormLabel className="font-normal">
                          Send email now
                        </FormLabel>
                        <p className="text-xs text-muted-foreground">
                          Off, or with no mail server configured, you hand the
                          link over yourself.
                        </p>
                      </div>
                      <FormControl>
                        <Switch
                          checked={field.value}
                          onCheckedChange={field.onChange}
                        />
                      </FormControl>
                    </FormItem>
                  )}
                />
              </fieldset>

              <fieldset
                className="grid min-w-0 gap-4 py-4"
                hidden={step !== "provisioning"}
                disabled={mutation.isPending}
              >
                <div className="space-y-2">
                  <Label>AI credentials</Label>
                  <p className="text-xs text-muted-foreground">
                    Pre-selected from what a {userRoleLabel(role)} is
                    auto-provisioned. Adjust for this person only.
                  </p>
                  {credentialsPending && (
                    <p className="text-sm text-muted-foreground">
                      Loading credentials…
                    </p>
                  )}
                  {credentialsError && (
                    <p className="text-sm text-muted-foreground">
                      Could not load managed credentials. The invitation will
                      still be sent, and the account gets whatever a{" "}
                      {userRoleLabel(role)} is auto-provisioned.
                    </p>
                  )}
                  {credentials?.length === 0 && (
                    <p className="text-sm text-muted-foreground">
                      No managed AI credentials exist yet. Add one on the LLM
                      Providers page.
                    </p>
                  )}
                  {/* A selection checklist in a wizard step, not a list in a
                      card: it must show every credential the admin can grant,
                      so its length is bounded by scroll rather than by a cap
                      that would hide choices. */}
                  <div className="max-h-[40vh] space-y-2 overflow-y-auto">
                    {(credentials ?? []).map((record) => {
                      const selected = (credentialIds ?? suggestedIds).includes(
                        record.id,
                      )
                      return (
                        <div key={record.id} className="flex items-start gap-2">
                          <Checkbox
                            id={`credential-${record.id}`}
                            checked={selected}
                            onCheckedChange={(checked) =>
                              toggleCredential(record.id, checked === true)
                            }
                            className="mt-0.5"
                          />
                          <Label
                            htmlFor={`credential-${record.id}`}
                            className="min-w-0 flex-1 cursor-pointer font-normal"
                          >
                            {record.name}
                            {record.default_model && (
                              <span className="text-muted-foreground">
                                {" "}
                                · {record.default_model}
                              </span>
                            )}
                          </Label>
                        </div>
                      )
                    })}
                  </div>
                </div>

                <div className="flex items-start gap-2 border-t pt-4">
                  <Checkbox
                    id="invite-include-desktop"
                    checked={includeDesktop}
                    onCheckedChange={(checked) =>
                      setIncludeDesktopChoice(checked === true)
                    }
                    className="mt-0.5"
                  />
                  <div className="min-w-0 flex-1">
                    <Label
                      htmlFor="invite-include-desktop"
                      className="cursor-pointer font-normal"
                    >
                      Also invite to Cinna Desktop
                    </Label>
                    <p className="text-xs text-muted-foreground">
                      Points them at the desktop app once they have claimed the
                      account.
                    </p>
                  </div>
                </div>
              </fieldset>

              <DialogFooter>
                {step === "who" ? (
                  <>
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => handleOpenChange(false)}
                    >
                      Cancel
                    </Button>
                    <Button type="button" onClick={goToProvisioning}>
                      Next
                    </Button>
                  </>
                ) : (
                  <>
                    <Button
                      type="button"
                      variant="outline"
                      disabled={mutation.isPending}
                      onClick={() => setStep("who")}
                    >
                      Back
                    </Button>
                    <LoadingButton type="submit" loading={mutation.isPending}>
                      Send invitation
                    </LoadingButton>
                  </>
                )}
              </DialogFooter>
            </form>
          </Form>
        )}
      </DialogContent>
    </Dialog>
  )
}

export default InviteUserDialog
