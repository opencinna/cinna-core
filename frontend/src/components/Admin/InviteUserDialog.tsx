import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Copy,
  KeyRound,
  Mail,
  MailX,
  UserPlus,
} from "lucide-react"
import { useMemo, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  AdminLlmProvidersService,
  type AIKeyOnboardingState,
  type InviteUserRequest,
  type InviteUserResponse,
  type ManagedAICredentialPublic,
  ServerConfigService,
  UsersService,
} from "@/client"
import { ManagedCredentialDialog } from "@/components/Admin/LlmProviders/ManagedCredentialDialog"
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

/**
 * Copy for every `reason` `InviteProvisioningSkip` can carry.
 *
 * The vocabulary is enumerated on the backend model. `managed_credential_not_found`
 * is reachable only from this path — the explicit-list entry point — so it is
 * the one most likely to be missing from a hand-written map.
 */
const SKIP_REASON_COPY: Record<string, string> = {
  user_not_found: "the new account could not be read back",
  user_inactive: "the account is not active",
  managed_credential_not_found: "that credential no longer exists",
  provision_failed: "provisioning failed for that credential",
  add_members_failed: "the grant itself failed",
}

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
          <InviteSuccess
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
                <DialogTitle>
                  {step === "who" ? "Invite user" : "Provisioning"}
                </DialogTitle>
                <DialogDescription>
                  {step === "who"
                    ? "Create the account and send its owner a link to claim it. No password is set here — they choose how to sign in."
                    : `What ${form.getValues("email") || "the new account"} starts with.`}
                </DialogDescription>
              </DialogHeader>

              <div className="grid gap-4 py-4" hidden={step !== "who"}>
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
              </div>

              <div className="grid gap-4 py-4" hidden={step !== "provisioning"}>
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
                  <div className="space-y-2">
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
              </div>

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

/**
 * What happened, and the link — which is the whole point on an instance with
 * no SMTP, the default for a fresh install.
 */
function InviteSuccess({
  result,
  onDone,
  onInviteAnother,
}: {
  result: InviteUserResponse
  onDone: () => void
  onInviteAnother: () => void
}) {
  const [copied, setCopied] = useState(false)
  const { showErrorToast } = useCustomToast()

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(result.accept_url)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      showErrorToast("Could not copy — select the link and copy it manually.")
    }
  }

  const added = result.provisioning.added_count ?? 0
  const skipped = result.provisioning.skipped ?? []
  // Provisioning falling over as a whole and an admin deliberately ticking
  // nothing both arrive as `added_count=0, skipped=[]`. Only this flag tells
  // them apart, and they must never render the same again: an admin whose
  // grant transiently failed read "No AI credentials were granted" and
  // concluded their ticks had not saved.
  const provisioningFailed = result.provisioning.provisioning_failed ?? false
  // The third state, and it arrives the same way the second one did: as an
  // empty-looking report. Provisioning short-circuits on an inactive account
  // — granting keys to an account nobody can sign into buys nothing — and it
  // now says so, one `user_inactive` skip per credential the admin ticked,
  // instead of returning the report an admin who ticked nothing gets.
  //
  // Reachable through adoption: re-inviting an account an administrator
  // deliberately deactivated no longer reactivates it, so the ticks are
  // genuinely not applied and the admin has to be told once, plainly, rather
  // than being handed N identical "one credential was skipped" lines.
  //
  // Whether the account is active is read off the account, not inferred from
  // the skip reasons: it is a fact about the row and the response carries it,
  // and deriving it a second time from the report would be two answers to one
  // question that disagree the first time the report changes.
  const accountInactive = result.user.is_active === false
  const inactiveSkips = accountInactive
    ? skipped.filter((skip) => skip.reason === "user_inactive")
    : []
  // Everything else still gets its per-credential line. The inactive ones do
  // not: they are the summary above, N times over. Filtered by *value*, not
  // by membership of `inactiveSkips` — an identity test survives only as long
  // as nobody maps, clones or memoises `skipped` between these two lines, and
  // the day someone does, every inactive skip silently comes back as a
  // duplicate line under the summary.
  const detailSkips = skipped.filter(
    (skip) => !(accountInactive && skip.reason === "user_inactive"),
  )
  // `add_members` is idempotent and deliberately does not report ids that
  // were already members, and `provision_explicit` reads only `added`. So an
  // adopted account that the interrupted attempt had already provisioned
  // comes back as `added_count=0` while holding the credentials — "none were
  // granted" would be false about it. "No new" is true in both adoption
  // cases: the resumed invite and the channel-created sender.
  const noneGrantedCopy = result.adopted_existing_account
    ? "No new AI credentials were granted."
    : "No AI credentials were granted."

  return (
    <>
      <DialogHeader>
        <DialogTitle>Invitation created</DialogTitle>
        <DialogDescription>
          {/* The address may already have had an account — an interrupted
              earlier invite, or the passwordless row a server channel created
              for an inbound sender. Announcing a fresh account for one of
              those is wrong, so say which happened. */}
          {result.adopted_existing_account
            ? `${result.user.email} already had an account. The invitation was attached to it rather than creating a new one.`
            : `${result.user.email} now has an account waiting to be claimed.`}
        </DialogDescription>
      </DialogHeader>

      <div className="grid gap-4 py-4">
        <div className="flex items-start gap-2 text-sm">
          {result.email_sent ? (
            <>
              <Mail className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
              <span>The invitation email has been sent.</span>
            </>
          ) : (
            <>
              <MailX className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
              {/* A deactivated account is one of the reasons the server
                  deliberately suppresses the mail — the recipient could not
                  redeem the link yet, and the refusal they would get is by
                  design indistinguishable from a forgery. Telling this admin
                  to hand the link over would be telling them to hand over a
                  dud. */}
              <span>
                {accountInactive
                  ? "No email was sent — the account is deactivated, so the link cannot be redeemed yet."
                  : "No email was sent. Hand the link below over yourself."}
              </span>
            </>
          )}
        </div>

        <div className="space-y-2">
          <Label>Invitation link</Label>
          <div className="flex items-center gap-2">
            <Input
              readOnly
              value={result.accept_url}
              className="font-mono text-xs"
            />
            <Button
              type="button"
              variant="outline"
              size="icon"
              onClick={copy}
              aria-label="Copy invitation link"
            >
              {copied ? <Check /> : <Copy />}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            This link signs the person in as {result.user.email} — share it
            privately.
            {accountInactive &&
              " It starts working once the account is activated."}
          </p>
        </div>

        <AddKeyStep result={result} />

        <div className="space-y-1 border-t pt-4 text-sm">
          {provisioningFailed ? (
            // Styled apart as well as worded apart: this line is scanned, not
            // read, and the failure has to survive the glance.
            <div className="flex items-start gap-2 text-destructive">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" />
              <p>
                AI credential provisioning failed — the grant did not complete.
                The invitation is unaffected; check the AI Credentials page
                and grant them there.
              </p>
            </div>
          ) : accountInactive ? (
            // Gated on the *fact*, not on the skip count. An inactive account
            // can report zero skips and still be the reason nothing was
            // granted: `onSubmit` sends `managed_credential_ids: null` while
            // the credential query is in flight or has failed, the server
            // then has no ids to name, and falling through to "No AI
            // credentials were granted" would be the exact screen this branch
            // exists to stop showing.
            //
            // Not a failure and not "nothing was asked for", so neither of the
            // other two renderings is honest about it. Amber, not destructive:
            // nothing went wrong, the account simply cannot hold keys yet.
            <div className="flex items-start gap-2 text-amber-600 dark:text-amber-400">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" />
              <p>
                This account is deactivated, so{" "}
                {/* "selected" means requested, not existing: the short-circuit
                    runs before the credentials are looked up, so an id that
                    has since been deleted is counted here rather than
                    reported as `managed_credential_not_found`. */}
                {inactiveSkips.length === 0
                  ? "no AI credentials were granted"
                  : inactiveSkips.length === 1
                    ? "the AI credential you selected was not granted"
                    : `the ${inactiveSkips.length} AI credentials you selected were not granted`}
                . Activate the account, then use “Apply to existing users” on
                the AI Credentials page.
              </p>
            </div>
          ) : (
            <p>
              {added === 0
                ? noneGrantedCopy
                : `${added} AI credential${added === 1 ? "" : "s"} granted.`}
            </p>
          )}
          {detailSkips.map((skip) => (
            <p
              key={skip.managed_credential_id}
              className="text-xs text-muted-foreground"
            >
              One credential was skipped —{" "}
              {SKIP_REASON_COPY[skip.reason] ?? skip.reason}.
            </p>
          ))}
          {result.invitation.include_desktop && (
            <p className="text-xs text-muted-foreground">
              Cinna Desktop is offered after they claim the account.
            </p>
          )}
        </div>
      </div>

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onInviteAnother}>
          Invite another
        </Button>
        <Button type="button" onClick={onDone}>
          Done
        </Button>
      </DialogFooter>
    </>
  )
}

/**
 * Step 3 — give this person a key.
 *
 * The step the wizard could not have at step 2: `target_user_ids` needs a user
 * id and no account existed yet. It exists by the time this renders, because
 * this renders from the invite mutation's own response — `result.user` is a
 * `UserPublic`, so the id is in hand.
 *
 * This is how an account gets a key for any provider whose administration API
 * does not create keys, which includes the default provider on most instances.
 * It is a normal way to finish an invitation, not a consolation for something
 * that did not work — nothing in this copy frames it as one.
 *
 * It never blocks the invitation: the invite has already been sent by the time
 * this is on screen, and a failure here is its own failure with its own
 * message.
 *
 * It reuses `ManagedCredentialDialog` rather than growing a second paste form,
 * which would be a second answer to "what does creating an AI credential for
 * somebody involve" — per-provider field rules, Test Connection and the model
 * picker included.
 */
function AddKeyStep({ result }: { result: InviteUserResponse }) {
  const [isOpen, setIsOpen] = useState(false)
  // The *server's* answer for this person after the key was added, not our
  // inference from having created a row. `set_as_default` defaults to false, so
  // an admin can create a perfectly good credential and leave the person on the
  // paste-a-key wall with that exact credential listed in their settings —
  // which is what this used to announce as "now has their own AI credential".
  // Same field, same predicate, as the wall itself reads.
  const [keyState, setKeyState] = useState<AIKeyOnboardingState | null>(null)
  const added = keyState !== null

  // Provisioning short-circuits on a deactivated account, so offering the step
  // here would be offering something that silently does nothing. The screen
  // already says to activate the account first.
  if (result.user.is_active === false) return null

  return (
    <div className="space-y-2 rounded-md border p-3">
      <div className="flex items-start gap-2">
        <KeyRound className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        <div className="space-y-0.5">
          <p className="text-sm font-medium">Add a key for this user</p>
          <p className="text-xs text-muted-foreground">
            {!added
              ? `Give ${result.user.email} an API key of their own. They are already selected as its only member.`
              : keyState === "has_key"
                ? `${result.user.email} now has their own AI credential and can start working.`
                : keyState === "preparing"
                  ? `The key for ${result.user.email} is being created now. They will be able to work as soon as it lands — nothing further is needed from you.`
                  : `The credential was created, but it is not ${result.user.email}'s default, so nothing will pick it up yet. Set it as their default from the LLM Providers page — or they can choose it themselves in Settings.`}
          </p>
        </div>
      </div>
      {added ? (
        <p
          className={
            keyState === "needs_key"
              ? "flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400"
              : "flex items-center gap-1.5 text-xs text-emerald-600 dark:text-emerald-400"
          }
        >
          <CheckCircle2 className="size-3.5" />
          {keyState === "needs_key" ? "Key added — not their default." : "Key added."}
        </p>
      ) : (
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={() => setIsOpen(true)}
        >
          Add a key
        </Button>
      )}
      <ManagedCredentialDialog
        mode="create"
        open={isOpen}
        onOpenChange={setIsOpen}
        initialTargets={[
          {
            id: result.user.id,
            userId: result.user.id,
            fallbackLabel: result.user.full_name
              ? `${result.user.full_name} <${result.user.email}>`
              : result.user.email,
          },
        ]}
        nameSubject={result.user.email}
        onCreated={(created) =>
          setKeyState(
            created.record.members?.find(
              (m) => m.user_id === result.user.id,
            )?.api_key_onboarding_state ?? "needs_key",
          )
        }
      />
    </div>
  )
}

export default InviteUserDialog
