import { Link } from "@tanstack/react-router"
import { AlertTriangle, Mail, MailX } from "lucide-react"

import type { AIProviderPublic, InviteUserResponse } from "@/client"
import { sdkModeLabel } from "@/components/Admin/LlmProviders/providerTypes"
import { CopyableValue } from "@/components/Common/CopyableValue"
import { Button } from "@/components/ui/button"
import {
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

/**
 * Copy for every `reason` `InviteProvisioningSkip` can carry.
 *
 * The vocabulary is enumerated on the backend model, and this map's **fallback
 * is part of its contract**: the render below is
 * `SKIP_REASON_COPY[skip.reason] ?? skip.reason`, so a reason with no entry
 * prints its own machine string inside an admin-facing sentence. That is what
 * makes a missing key a visible defect rather than a silent one, and it is why
 * the whole vocabulary is written out here rather than the subset this surface
 * expects to see. All five entries below are present; the fallback is the guard
 * for a sixth reason a later release adds.
 *
 * `provider_not_found` is reachable **only** from this path — explicit
 * provisioning is the only caller that can be handed an id — so it is the one
 * entry the automatic path could never have exercised, and the one this phase
 * existed to add.
 *
 * `managed_credential_not_found` is gone. Nothing produces it since the wizard
 * moved to providers, and a dead key in a copy map is how the next reader
 * concludes the old vocabulary is still live.
 */
const SKIP_REASON_COPY: Record<string, string> = {
  user_not_found: "the new account could not be read back",
  user_inactive: "the account is not active",
  provider_not_found:
    "that provider no longer exists, or has no credential to grant through",
  provision_failed: "provisioning failed for that provider",
  add_members_failed: "the grant itself failed",
}

/**
 * What happened, and the link — which is the whole point on an instance with
 * no SMTP, the default for a fresh install.
 *
 * A success screen *links* onward instead of nesting the next step. Giving the
 * new account a key of its own used to happen here, in a second dialog opened
 * on top of this one; it now lives where credentials live, and the link below
 * lands on that page with this person already selected.
 */
export function InviteSuccessPanel({
  result,
  providers,
  onDone,
  onInviteAnother,
}: {
  result: InviteUserResponse
  /**
   * The providers the wizard listed, so both line families below can name one
   * instead of printing an id. The array the wizard already holds.
   */
  providers: AIProviderPublic[]
  onDone: () => void
  onInviteAnother: () => void
}) {
  /**
   * The provider's name, or `null` when the id names nothing this wizard saw.
   *
   * `null` is the honest answer and it is exactly what `provider_not_found`
   * produces, so the sentences below fall back to "One provider" rather than
   * inventing a name or printing a UUID.
   */
  const providerName = (id: string): string | null =>
    providers.find((provider) => provider.id === id)?.name ?? null

  const added = result.provisioning.added_count ?? 0
  const skipped = result.provisioning.skipped ?? []
  // A grant that landed while the account already held a default in that mode.
  //
  // **Disclosure, not an error** — the same category as
  // `adopted_existing_account`. The grant succeeded and the person holds a
  // usable key; the only surprising thing is that the provider's model wiring
  // did not apply, because an incumbent default was kept. It never sets
  // `provisioning_failed`, never joins `skipped`, and no count on this panel
  // includes it. Its whole risk is being read as a grant that fell over, so the
  // sentence below says the grant succeeded first.
  const defaultSlotSkips = result.provisioning.default_slot_skips ?? []
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

  // What the AI Credentials page shows for this person before its own lookup
  // resolves, and what it suggests as the credential's name.
  const personLabel = result.user.full_name || result.user.email

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
          <CopyableValue label="Invitation link" value={result.accept_url} />
          <p className="text-xs text-muted-foreground">
            This link signs the person in as {result.user.email} — share it
            privately.
            {accountInactive &&
              " It starts working once the account is activated."}
          </p>
        </div>

        <div className="space-y-1 border-t pt-4 text-sm">
          {provisioningFailed ? (
            // Styled apart as well as worded apart: this line is scanned, not
            // read, and the failure has to survive the glance.
            <div className="flex items-start gap-2 text-destructive">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" />
              <p>
                AI credential provisioning failed — the grant did not complete.
                The invitation is unaffected; check the AI Credentials page and
                grant them there.
              </p>
            </div>
          ) : accountInactive ? (
            // Gated on the *fact*, not on the skip count. An inactive account
            // can report zero skips and still be the reason nothing was
            // granted: the wizard sends `provider_ids: null` while
            // the provider query is in flight or has failed, the server then
            // has no ids to name, and falling through to "No AI credentials
            // were granted" would be the exact screen this branch exists to
            // stop showing.
            //
            // Not a failure and not "nothing was asked for", so neither of the
            // other two renderings is honest about it. Amber, not destructive:
            // nothing went wrong, the account simply cannot hold keys yet.
            <div className="flex items-start gap-2 text-warning">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" />
              <p>
                This account is deactivated, so{" "}
                {/* "selected" means requested, not existing: the short-circuit
                    runs before the providers are looked up, so an id that has
                    since been deleted is counted here rather than reported as
                    `provider_not_found`. */}
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
          {detailSkips.map((skip) => {
            const name = providerName(skip.provider_id)
            return (
              <p
                key={`${skip.provider_id}-${skip.reason}`}
                className="text-xs text-muted-foreground"
              >
                {name ? `"${name}" was skipped` : "One provider was skipped"} —{" "}
                {SKIP_REASON_COPY[skip.reason] ?? skip.reason}.
              </p>
            )
          })}
          {/* Beside the grant, in the same muted class as the skip lines —
              and gated, not merely placed, so "was granted" cannot appear under
              a headline saying provisioning failed or that the account cannot
              hold keys. The backend does not set `provisioning_failed` for a
              declined slot, but nothing in this component depended on that
              until the gate below made it explicit. */}
          {!provisioningFailed &&
            !accountInactive &&
            defaultSlotSkips.map((slot) => {
              const name = providerName(slot.provider_id)
              return (
                <p
                  key={`${slot.provider_id}-${slot.mode}`}
                  className="text-xs text-muted-foreground"
                >
                  {name ? `"${name}" was granted` : "A provider was granted"},
                  but it did not become the {sdkModeLabel(slot.mode)} default —
                  the account already had one, and it was kept.
                </p>
              )
            })}
          {result.invitation.include_desktop && (
            <p className="text-xs text-muted-foreground">
              Cinna Desktop is offered after they claim the account.
            </p>
          )}
        </div>

        {/* Hidden for a deactivated account for the same reason provisioning
            short-circuits on one: the offer would land on a page that cannot
            do anything for this person yet. */}
        {!accountInactive && (
          <div>
            <Button asChild variant="link" className="h-auto px-0">
              <Link
                to="/admin/ai-credentials"
                search={{
                  newCredentialFor: result.user.id,
                  label: personLabel,
                }}
              >
                Add an AI key for {result.user.email}
              </Link>
            </Button>
          </div>
        )}
      </div>

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onInviteAnother}>
          Invite another
        </Button>
        {/* Closes back onto the users list, whose table has just been
            invalidated and now carries the new row. There is no navigation
            here on purpose: this dialog's only host *is* that list. */}
        <Button type="button" onClick={onDone}>
          Done
        </Button>
      </DialogFooter>
    </>
  )
}
