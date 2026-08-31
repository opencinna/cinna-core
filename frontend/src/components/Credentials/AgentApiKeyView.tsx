import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { formatDistanceToNow } from "date-fns"
import {
  Check,
  Copy,
  Eye,
  EyeOff,
  FileJson,
  KeyRound,
  Lock,
} from "lucide-react"
import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import type { AgentApiKeyPublic, CredentialWithData } from "@/client"
import { AgentApiService, AgentsService, CredentialsService } from "@/client"
import { AgentApiScopeEditor } from "@/components/Common/AgentApiScopeEditor"
import { AgentBadge } from "@/components/Common/AgentBadge"
import {
  AGENT_API_KEY_LABEL,
  agentApiKeyScopeNote,
  buildAgentApiKeyCurl,
} from "@/components/Credentials/agentApiKeyCopy"
import { takeMintedAgentApiKey } from "@/components/Credentials/agentApiKeyMintHandoff"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
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
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { getErrorMessage, handleError } from "@/utils"
import { openAgentApiSpec } from "@/utils/agentApiSpec"

const formSchema = z.object({
  name: z.string().min(1, { message: "Name is required" }),
  notes: z.string().optional(),
})

type FormData = z.infer<typeof formSchema>

/**
 * Tolerance for browser-vs-server clock disagreement when reasoning about an
 * expiry we did not evaluate ourselves.
 */
const CLOCK_SKEW_MS = 60_000

interface AgentApiKeyViewProps {
  credential: CredentialWithData
  /** The key row behind this credential (producer key list). */
  apiKey: AgentApiKeyPublic
  producerAgentId: string
  /**
   * The user just minted this key (``?new=1``). Shows the value once so the
   * mint flow ends where the deliverable is, without a second click — read
   * from the mint response the flow parked in ``agentApiKeyMintHandoff``, not
   * from the audited reveal endpoint (plan D4).
   */
  justCreated?: boolean
}

/**
 * Detail view for an agent-api **external key**.
 *
 * The presence of this card is itself the marker of which of the two agent-api
 * modes you are looking at (plan D10.4): a *connection* is anonymous by
 * construction, so it has no single identity to show and gets
 * ``AgentApiConnectionView`` instead.
 *
 * Laid out as one full-width card over two half-width ones, so the thing you
 * came for is not buried under configuration:
 *
 * - **{AGENT_API_KEY_LABEL}** (full width) — the value, the base URL, and the
 *   curl to try it, and nothing else. The field is always on screen but the
 *   value is **masked by default**, showing only the public prefix so you can
 *   tell which key this is without disclosing it. Unlike a connection's machine
 *   token a key IS the deliverable, so it stays revealable — through the
 *   dedicated audited endpoint, one call per click (plan D4).
 * - **Details** (half) — name and notes. Naming affects nothing the key can do.
 * - **Access** (half) — what the key is bound to: the producer agent (fixed at
 *   mint), the subject **identity** (read-only, because a token already in
 *   someone's hands must never quietly start acting as a different user — plan
 *   D6; to point a key at someone else you revoke it and issue a new one, which
 *   is honest about the consequence), and **scopes** (fully editable, written
 *   straight to the
 *   ``(producer, subject)`` grant — this card and the producer's Access &
 *   Scopes card are two views of one row, plan D5, which is why the D7 note
 *   has to be here).
 */
export function AgentApiKeyView({
  credential,
  apiKey,
  producerAgentId,
  justCreated,
}: AgentApiKeyViewProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // ALWAYS the public URL the backend minted. AGENT_ENV_BACKEND_URL is an
  // env-sync-only rewrite for containers, and an external holder is not on the
  // Docker network.
  const baseUrl = (credential.credential_data?.base_url as string) ?? ""

  // Producer name for the D7 scope note. Uses the shared ["agent", id] key, so
  // it is usually a warm cache hit from the producer's own page. Best-effort:
  // the note reads fine without it.
  const { data: producer } = useQuery({
    queryKey: ["agent", producerAgentId],
    queryFn: () => AgentsService.readAgent({ id: producerAgentId }),
    retry: false,
  })

  const subjectLabel =
    apiKey.subject?.full_name || apiKey.subject?.email || "Unknown user"
  const producerName = producer?.name || "this agent's API"

  const expiresAtMs = apiKey.expires_at
    ? new Date(apiKey.expires_at).getTime()
    : null
  const expired = expiresAtMs !== null && expiresAtMs <= Date.now()
  // `is_usable` = active AND not expired AND the producer's external-access
  // switch is on. Everything but that last term is visible here, so an
  // otherwise-healthy key that is not usable can only mean the switch is off.
  // Worth naming, because that one is fixed on the agent, not here.
  //
  // The near-expiry guard keeps clock skew from mislabelling a server-expired
  // key as "Blocked" and pointing the owner at the wrong fix.
  const nearExpiry =
    expiresAtMs !== null && expiresAtMs - Date.now() < CLOCK_SKEW_MS
  const blockedByProducer =
    apiKey.is_active && !expired && !nearExpiry && !apiKey.is_usable

  // ── Metadata (name / notes) ───────────────────────────────────────────────
  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    defaultValues: { name: credential.name, notes: credential.notes ?? "" },
  })

  useEffect(() => {
    form.reset({ name: credential.name, notes: credential.notes ?? "" })
  }, [credential, form])

  const metadataMutation = useMutation({
    mutationFn: (data: FormData) =>
      CredentialsService.updateCredential({
        id: credential.id,
        requestBody: data,
      }),
    // Mark the form pristine from the saved values instead of invalidating
    // ["credential-with-data"]. Renaming a key changes nothing that query
    // carries for a key (the value is not in it — plan D4), so a refetch would
    // buy a round trip and a re-render of the whole detail page for nothing.
    onSuccess: (_res, variables) => {
      showSuccessToast("Key updated")
      form.reset(variables)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({ queryKey: ["credential", credential.id] })
    },
  })

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="h-5 w-5" />
            {AGENT_API_KEY_LABEL}
            {apiKey.read_only && (
              <Badge variant="outline" className="gap-1 text-xs">
                <Lock className="h-3 w-3" />
                read-only
              </Badge>
            )}
            {expired ? (
              <Badge variant="outline" className="text-xs text-destructive">
                Expired
              </Badge>
            ) : blockedByProducer ? (
              <Badge variant="outline" className="text-xs text-amber-600">
                Blocked
              </Badge>
            ) : (
              !apiKey.is_usable && (
                <Badge variant="outline" className="text-xs text-destructive">
                  Inactive
                </Badge>
              )
            )}
          </CardTitle>
          <CardDescription>
            Lets code outside the platform call this agent's REST API as{" "}
            <span className="font-medium text-foreground">{subjectLabel}</span>.
            Delete this credential — or revoke the key on the producer's card —
            to kill it instantly.
          </CardDescription>
        </CardHeader>

        <CardContent className="space-y-6">
          {blockedByProducer && (
            <p className="rounded-md border border-amber-300 bg-amber-50 dark:bg-amber-950/30 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
              External access is currently off on the producer agent, so this
              key is refused at the proxy. Turn "External Keys" back on from the
              agent's Agent REST API card to restore it.
            </p>
          )}

          {/* The value plus everything needed to call with it — this card holds
              nothing else. Who the key is and what it may do live in the
              "Access" card below, so the thing you came here to copy is not
              buried under configuration. */}
          {/* Keyed on the credential, and load-bearing: the route does NOT
              remount when `credentialId` changes, so navigating straight from
              one key's page to another would otherwise reuse this instance —
              which holds a revealed value in local state — and show the old
              key's value under the new key's identity. */}
          <KeyUsageSection
            key={credential.id}
            credentialId={credential.id}
            tokenPrefix={apiKey.token_prefix}
            baseUrl={baseUrl}
            producerAgentId={producerAgentId}
            justCreated={!!justCreated}
          />
        </CardContent>
      </Card>

      {/* Two half-width cards: what you can rename (left) and what the key is
          bound to (right). Stacks on narrow screens. */}
      <div className="grid gap-6 lg:grid-cols-2 items-start">
        {/* Metadata — name + notes only; the value is never edited by hand. */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Details</CardTitle>
            <CardDescription>
              Naming is for you — it does not affect what the key can do.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Form {...form}>
              <form
                onSubmit={form.handleSubmit((d) => metadataMutation.mutate(d))}
                className="space-y-4"
              >
                <FormField
                  control={form.control}
                  name="name"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>
                        Name <span className="text-destructive">*</span>
                      </FormLabel>
                      <FormControl>
                        <Input placeholder="Key name" type="text" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="notes"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Notes</FormLabel>
                      <FormControl>
                        <Textarea
                          placeholder="Where this key is deployed, who holds it…"
                          className="min-h-[100px]"
                          {...field}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                {form.formState.isDirty && (
                  <div className="flex justify-end gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => form.reset()}
                      disabled={metadataMutation.isPending}
                    >
                      Reset
                    </Button>
                    <LoadingButton
                      type="submit"
                      loading={metadataMutation.isPending}
                    >
                      Save Changes
                    </LoadingButton>
                  </div>
                )}
              </form>
            </Form>
          </CardContent>
        </Card>

        {/* Access — what the key is bound to: which API, whose identity, which
          capabilities. The producer and the subject are both fixed at mint and
          cannot be edited here (changing either means revoking this key and
          issuing a new one); scopes ARE editable, but they live on the
          producer's grant rather than on the key. */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Access</CardTitle>
            <CardDescription>
              What this key can reach, and who it acts as. The agent and the
              user are fixed for the key's whole life — to change either, revoke
              this key and issue a new one.
            </CardDescription>
          </CardHeader>
          {/* Label column fixed rather than `auto`, so all four value columns
              line up with each other instead of shifting with the longest
              label. */}
          <CardContent className="grid grid-cols-[4.5rem_1fr] items-start gap-x-3 gap-y-4">
            <AccessLabel>Agent</AccessLabel>
            <div className="flex min-w-0 justify-end">
              <AgentBadge
                agent={{
                  id: producerAgentId,
                  name: producerName,
                  ui_color_preset: producer?.ui_color_preset,
                }}
                linkTo="agent"
              />
            </div>

            <AccessLabel>Acts as</AccessLabel>
            <IdentityRow
              subjectLabel={subjectLabel}
              subjectEmail={apiKey.subject?.email ?? null}
            />

            {/* Label and value rendered as a pair: KeyScopesSection returns
                null without a subject, and a lone label in a two-column grid
                would shift every following row into the wrong column.
                This is also the one value cell that is NOT right-aligned —
                it is a chip editor with a wrapping explanatory note, and
                right-aligning wrapped prose gives it a ragged left edge. */}
            {apiKey.subject?.id && (
              <>
                <AccessLabel>Scopes</AccessLabel>
                <KeyScopesSection
                  producerAgentId={producerAgentId}
                  subjectUserId={apiKey.subject.id}
                  subjectLabel={subjectLabel}
                  producerLabel={producerName}
                />
              </>
            )}

            <AccessLabel>Expires</AccessLabel>
            <div className="min-w-0 text-right text-sm">
              {apiKey.expires_at
                ? `${new Date(apiKey.expires_at).toLocaleDateString()} (${formatDistanceToNow(
                    new Date(apiKey.expires_at),
                    { addSuffix: true },
                  )})`
                : "Never"}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

// ── Identity ────────────────────────────────────────────────────────────────

/**
 * Left-hand label of an Access row. `pt-1` optically centres it against the
 * first line of a `text-sm` value, which is a hair taller.
 */
function AccessLabel({ children }: { children: React.ReactNode }) {
  return <div className="pt-1 text-xs text-muted-foreground">{children}</div>
}

interface IdentityRowProps {
  subjectLabel: string
  subjectEmail: string | null
}

/**
 * Who the key acts as. Fixed at mint and never editable here (plan D6) — stated
 * once in the card's description rather than repeated under the field.
 */
function IdentityRow({ subjectLabel, subjectEmail }: IdentityRowProps) {
  return (
    <div className="min-w-0 text-right">
      <div className="truncate text-sm font-medium">{subjectLabel}</div>
      {subjectEmail && subjectEmail !== subjectLabel && (
        <div className="truncate text-xs text-muted-foreground">
          {subjectEmail}
        </div>
      )}
    </div>
  )
}

// ── Scopes ──────────────────────────────────────────────────────────────────

interface KeyScopesSectionProps {
  producerAgentId: string
  subjectUserId: string | null
  subjectLabel: string
  producerLabel: string
}

/**
 * Scope editor writing to the ``(producer, subject)`` grant — the same row the
 * producer's Access & Scopes card edits (plan D5). Creates the grant on first
 * save, updates it afterwards.
 */
function KeyScopesSection({
  producerAgentId,
  subjectUserId,
  subjectLabel,
  producerLabel,
}: KeyScopesSectionProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: grantsData } = useQuery({
    queryKey: ["agentApiGrants", producerAgentId],
    queryFn: () =>
      AgentApiService.listAgentApiGrants({ agentId: producerAgentId }),
    enabled: !!subjectUserId,
  })
  const { data: catalogData } = useQuery({
    queryKey: ["agentApiScopeCatalog", producerAgentId],
    queryFn: () =>
      AgentApiService.getAgentApiScopeCatalog({ agentId: producerAgentId }),
  })

  const grant = grantsData?.data.find((g) => g.user_id === subjectUserId)
  const savedScopes = grant?.scopes ?? []

  // Local draft, re-seeded whenever the server value changes identity. Tracking
  // the serialized server value (not just the grant id) means an edit made from
  // the producer's card lands here on the next refetch instead of being masked
  // by a stale draft.
  const savedKey = `${grant?.id ?? "none"}:${savedScopes.join(",")}`
  const [draft, setDraft] = useState<string[]>(savedScopes)
  const [seededFrom, setSeededFrom] = useState(savedKey)
  if (savedKey !== seededFrom) {
    setSeededFrom(savedKey)
    setDraft(savedScopes)
  }

  const isDirty =
    draft.length !== savedScopes.length ||
    draft.some((s, i) => s !== savedScopes[i])

  const saveMutation = useMutation({
    mutationFn: (scopes: string[]) =>
      grant
        ? AgentApiService.updateAgentApiGrant({
            agentId: producerAgentId,
            grantId: grant.id,
            requestBody: { scopes },
          })
        : AgentApiService.createAgentApiGrant({
            agentId: producerAgentId,
            requestBody: { user_id: subjectUserId as string, scopes },
          }),
    onSuccess: () => {
      showSuccessToast("Scopes updated — effective on the next call")
      queryClient.invalidateQueries({
        queryKey: ["agentApiGrants", producerAgentId],
      })
    },
    onError: (e: any) =>
      showErrorToast(e?.message || "Failed to update scopes"),
  })

  if (!subjectUserId) return null

  return (
    // No label of its own — the Access grid's left column provides it.
    <div className="min-w-0 space-y-2">
      <AgentApiScopeEditor
        scopes={draft}
        onChange={setDraft}
        catalogScopes={catalogData?.scopes ?? []}
        emptyHint="No scopes — the caller is identified but carries no capabilities."
        disabled={saveMutation.isPending}
      />
      {/* Plan D7: narrower than "application wide" on purpose. */}
      <p className="text-xs text-muted-foreground">
        {agentApiKeyScopeNote(subjectLabel, producerLabel)}
      </p>
      {isDirty && (
        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setDraft(savedScopes)}
            disabled={saveMutation.isPending}
          >
            Reset
          </Button>
          <LoadingButton
            size="sm"
            loading={saveMutation.isPending}
            onClick={() => saveMutation.mutate(draft)}
          >
            Save scopes
          </LoadingButton>
        </div>
      )}
    </div>
  )
}

// ── Value + how to call it ──────────────────────────────────────────────────

interface KeyUsageSectionProps {
  credentialId: string
  /** The key's public prefix — what the masked placeholder shows. */
  tokenPrefix: string
  baseUrl: string
  producerAgentId: string
  /** ``?new=1``: the value was just minted and is waiting in the handoff. */
  justCreated: boolean
}

/**
 * The value, its base URL, and a runnable curl.
 *
 * The value is masked until the user asks for it, and asking FETCHES it —
 * ``POST /credentials/{id}/agent-api-key/reveal``, the one path that returns a
 * key after mint and the one that writes ``AGENT_API_EXTERNAL_KEY_REVEALED``
 * (plan D4). Two consequences shape this component:
 *
 * - It must be a **mutation, not a query**. A query would re-run on remount and
 *   window refocus, and every one of those would be a high-severity security
 *   event for a disclosure nobody asked for. One click, one call, one event.
 * - Hiding **drops** the value rather than keeping it behind a display toggle,
 *   so the next Reveal is a real, audited disclosure too. The alternative — a
 *   cached value silently re-shown — makes the audit trail undercount who saw
 *   what, which is the whole thing the endpoint is there to prevent.
 *
 * The one reveal that does NOT call the endpoint is the post-mint one: that
 * value comes from the mint response via ``agentApiKeyMintHandoff``, because
 * auditing a disclosure the user was just handed is noise, not signal.
 */
function KeyUsageSection({
  credentialId,
  tokenPrefix,
  baseUrl,
  producerAgentId,
  justCreated,
}: KeyUsageSectionProps) {
  // `null` = masked. The value only ever lives here, for as long as it is on
  // screen.
  const [token, setToken] = useState<string | null>(null)

  // Claim the just-minted value, if the mint flow parked one. Always drains
  // (one-shot, so a later visit to this page in the same tab cannot re-show a
  // stale value) but only displays it when the route says this really is the
  // step right after minting.
  //
  // The absence of an `else setToken(null)` is load-bearing, not an oversight:
  // StrictMode runs this effect twice, and the second pass finds the slot
  // already drained. Clearing on a miss would wipe the value the first pass
  // just set.
  useEffect(() => {
    const minted = takeMintedAgentApiKey(credentialId)
    if (minted && justCreated) setToken(minted)
  }, [credentialId, justCreated])

  // Reported inline rather than as a toast: the failure belongs to the control
  // the user just pressed, and a toast would be gone by the time they look for
  // the value that never appeared.
  const revealMutation = useMutation({
    mutationFn: () =>
      CredentialsService.revealAgentApiKey({ id: credentialId }),
    onSuccess: (data) => setToken(data.token),
  })

  // Copy must work whether or not the value is on screen, so when it is hidden
  // this fetches it just for the clipboard write. Deliberately NOT
  // `revealMutation.mutateAsync`: that would run its `onSuccess` and un-hide
  // the field, turning a copy into a reveal. The value is returned, used, and
  // dropped — never stored — so "hidden" keeps meaning the page is not holding
  // it. The audit event still fires, which is right: copying is a disclosure.
  const resolveToken = async () =>
    token ??
    (await CredentialsService.revealAgentApiKey({ id: credentialId })).token

  const revealed = token !== null
  const masked = `${tokenPrefix}${"•".repeat(24)}`

  return (
    <div className="space-y-3">
      <div className="space-y-2">
        <Label className="text-xs text-muted-foreground">Key</Label>
        {/* The field is always on screen; only the VALUE is hidden by default.
            Masked shows the public prefix, which identifies which key this is
            without disclosing anything — revealing is a separate, audited act. */}
        <div className="flex items-center gap-2">
          {/* Wraps once revealed: a key is longer than its mask, and truncating
              the one thing the page exists to hand over would make it
              unreadable to anyone not using the copy button. */}
          <code
            className={`flex-1 min-w-0 rounded-md border bg-muted/40 px-3 py-2 font-mono text-xs ${
              revealed ? "break-all" : "truncate"
            }`}
          >
            {token ?? masked}
          </code>
          <LoadingButton
            variant="outline"
            size="sm"
            className="shrink-0"
            loading={revealMutation.isPending}
            onClick={() => {
              if (!revealed) {
                revealMutation.mutate()
                return
              }
              // Drop the value AND the mutation's copy of it, so "hidden"
              // means the browser is not holding the key anywhere. Also clears
              // a failed attempt's inline error.
              setToken(null)
              revealMutation.reset()
            }}
          >
            {revealMutation.isPending ? null : revealed ? (
              <EyeOff className="h-4 w-4 mr-1.5" />
            ) : (
              <Eye className="h-4 w-4 mr-1.5" />
            )}
            {revealed ? "Hide" : "Reveal"}
          </LoadingButton>
          <CopyButton value={token} label="Copy key" resolve={resolveToken} />
        </div>
        {revealMutation.isError && (
          <p className="text-xs text-destructive">
            {getErrorMessage(
              revealMutation.error,
              "Could not reveal the key. Try again.",
            )}
          </p>
        )}
        <p className="text-xs text-muted-foreground">
          Treat it like a password: it grants this API's exposed surface to
          whoever holds it. Every reveal is recorded as a high-severity security
          event, so who had sight of the value stays auditable.
        </p>
      </div>

      <div className="space-y-2">
        <Label className="text-xs text-muted-foreground">Base URL</Label>
        <div className="flex items-center gap-2">
          <code className="flex-1 min-w-0 truncate rounded-md border bg-muted/40 px-3 py-2 font-mono text-xs">
            {baseUrl || "—"}
          </code>
          <CopyButton value={baseUrl} label="Copy base URL" />
          <Button
            variant="outline"
            size="sm"
            className="shrink-0"
            onClick={() => openAgentApiSpec(producerAgentId)}
            title="Open the endpoints this key can reach (rendered docs) in a new tab"
          >
            <FileJson className="h-4 w-4 mr-1.5" />
            View Spec
          </Button>
        </div>
      </div>

      <div className="space-y-2">
        <div className="flex items-center justify-between gap-2">
          <Label className="text-xs text-muted-foreground">Try it</Label>
          {/* Always copies a RUNNABLE curl, masked or not — what is rendered
              below while hidden carries a placeholder token, and putting that
              on the clipboard would hand over a command that 401s. `value` is
              null while hidden precisely so the resolve path runs: the builder
              always returns a string, so passing it here unconditionally would
              short-circuit `value ?? resolve()` and copy the placeholder. */}
          <CopyButton
            value={token ? buildAgentApiKeyCurl(baseUrl, token) : null}
            label="Copy curl"
            resolve={async () =>
              buildAgentApiKeyCurl(baseUrl, await resolveToken())
            }
          />
        </div>
        <pre className="overflow-x-auto rounded-md border bg-muted/40 px-3 py-2 font-mono text-xs">
          {buildAgentApiKeyCurl(baseUrl, token)}
        </pre>
      </div>
    </div>
  )
}

/** Copy-to-clipboard button with a short confirmation state. */
function CopyButton({
  value,
  label,
  resolve,
}: {
  value: string | null
  label: string
  /**
   * Supplies the value when it is not on screen, so Copy works whether or not
   * the key is revealed. Resolving fetches through the audited reveal endpoint
   * — correct, because copying a secret IS a disclosure and should be recorded
   * as one. The resolved value is used for the clipboard write only and is
   * never stored, so copying does not un-hide the field.
   */
  resolve?: () => Promise<string | null>
}) {
  const [copied, setCopied] = useState(false)
  const [busy, setBusy] = useState(false)
  const { showErrorToast } = useCustomToast()

  useEffect(() => {
    if (!copied) return
    const handle = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(handle)
  }, [copied])

  return (
    <Button
      variant="outline"
      size="sm"
      className="shrink-0"
      disabled={(!value && !resolve) || busy}
      aria-label={label}
      title={label}
      onClick={async () => {
        setBusy(true)
        try {
          // Resolving and writing are reported separately: a failed resolve is
          // a server error (e.g. 400 "this key's value is no longer stored"),
          // and blaming the clipboard for it sends the user looking in the
          // wrong place.
          let resolved = value
          if (!resolved && resolve) {
            try {
              resolved = await resolve()
            } catch (e) {
              showErrorToast(
                getErrorMessage(e, "Could not retrieve the value to copy."),
              )
              return
            }
          }
          if (!resolved) return
          await navigator.clipboard.writeText(resolved)
          setCopied(true)
        } catch {
          showErrorToast("Could not copy to clipboard")
        } finally {
          setBusy(false)
        }
      }}
    >
      {copied ? (
        <Check className="h-4 w-4 text-emerald-500" />
      ) : (
        <Copy className="h-4 w-4" />
      )}
    </Button>
  )
}
