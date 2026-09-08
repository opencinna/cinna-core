import { useQuery } from "@tanstack/react-query"
import { createFileRoute, redirect, useNavigate } from "@tanstack/react-router"
import { AlertTriangle } from "lucide-react"
import { useCallback, useEffect, useState } from "react"

import {
  AdminLlmProvidersService,
  type ManagedAICredentialPublic,
} from "@/client"
import { KeysTab } from "@/components/Admin/AiKeys/KeysTab"
import { ConnectProviderDialog } from "@/components/Admin/AiProviders/ConnectProviderDialog"
import { ProvidersTab } from "@/components/Admin/AiProviders/ProvidersTab"
import { ManagedCredentialDialog } from "@/components/Admin/LlmProviders/ManagedCredentialDialog"
import { MANAGED_CREDENTIALS_QUERY_PREFIX } from "@/components/Admin/LlmProviders/providerTypes"
import { HashTabs, type TabConfig } from "@/components/Common/HashTabs"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import useAuth, { isLoggedIn } from "@/hooks/useAuth"
import useCustomToast from "@/hooks/useCustomToast"
import { usePageHeader } from "@/routes/_layout"
import { APP_NAME } from "@/utils"

/**
 * The invitation flow's hand-off.
 *
 * `?newCredentialFor=<user id>&label=<display name>` means "an admin has just
 * invited this person and wants to give them a key". It exists so the success
 * screen of the invite wizard can *link* onward instead of opening a second
 * dialog on top of itself: the link has to land somewhere that does the thing,
 * not on a page where the admin has to find the person again.
 */
type AiCredentialsSearch = {
  newCredentialFor?: string
  label?: string
}

/** The two hash-addressable tabs. `keys` is `tabs[0]`.
 *
 * It was `managed-credentials` while the first tab listed records. Nothing in
 * `frontend/src` links to that hash, and `HashTabs` falls back to `tabs[0]` for
 * one it does not recognise, so an old bookmark still lands on this tab. */
type TabValue = "keys" | "providers"

export const Route = createFileRoute("/_layout/admin/ai-credentials")({
  component: AdminAiCredentials,
  validateSearch: (search: Record<string, unknown>): AiCredentialsSearch => ({
    newCredentialFor:
      typeof search.newCredentialFor === "string" && search.newCredentialFor
        ? search.newCredentialFor
        : undefined,
    label:
      typeof search.label === "string" && search.label
        ? search.label
        : undefined,
  }),
  head: () => ({
    meta: [
      {
        title: `AI Credentials - Admin - ${APP_NAME}`,
      },
    ],
  }),
  beforeLoad: async ({ context }) => {
    if (!isLoggedIn()) {
      throw redirect({ to: "/login" })
    }
    // context.user is populated by the _layout auth guard.
    const user = (context as any)?.user
    if (user && !user.is_superuser) {
      throw redirect({ to: "/" })
    }
  },
})

function AdminAiCredentials() {
  const { setHeaderContent } = usePageHeader()
  const { user } = useAuth()
  const { showSuccessToast } = useCustomToast()
  const search = Route.useSearch()
  const navigate = useNavigate()

  // The hand-off, latched out of the URL and then stripped from it, the same
  // way `credential/$credentialId` latches `?new=1`: a refresh or a Back must
  // not reopen a create dialog the admin has already dealt with, and the
  // marker must not survive into a link the admin copies.
  //
  // Latched per target rather than per mount, because the router reuses this
  // component across a search change — arriving here a second time, for a
  // different person, has to re-arm.
  const [handoffTarget, setHandoffTarget] = useState<string | null>(
    () => search.newCredentialFor ?? null,
  )
  const [handoff, setHandoff] = useState<{ id: string; label: string } | null>(
    () =>
      search.newCredentialFor
        ? { id: search.newCredentialFor, label: search.label ?? "" }
        : null,
  )
  // Tracks the marker's *absence* as well as its presence. The idiom this
  // copies latches on `credentialId`, a path identity that outlives the
  // `?new=1` marker; here the identity is the marker and the effect below
  // deletes it, so a latch that only moved on a new id would keep the last one
  // forever — and a second link for the *same* person would silently do
  // nothing.
  const marker = search.newCredentialFor ?? null
  if (marker !== handoffTarget) {
    setHandoffTarget(marker)
    if (marker) setHandoff({ id: marker, label: search.label ?? "" })
  }

  useEffect(() => {
    if (search.newCredentialFor !== undefined || search.label !== undefined) {
      navigate({
        to: "/admin/ai-credentials",
        search: {},
        // The hash and the hand-off marker have to coexist. This navigate
        // states the whole location, so omitting `hash` drops the one
        // `HashTabs` wrote and silently sends the admin back to the first tab
        // — arriving at `#providers` and dismissing a hand-off would land them
        // on Managed credentials. Read at the moment of the call rather than
        // from state, because `HashTabs` owns the hash and writes it directly
        // to `window.location`. `undefined` when there is none, so arriving
        // with `?newCredentialFor=` and no hash does not invent one.
        hash: window.location.hash.slice(1) || undefined,
        replace: true,
      })
    }
  }, [search.newCredentialFor, search.label, navigate])

  // What is still *owed* after a key was added — never a receipt.
  //
  // The three outcomes are not the same kind of fact. "Key added" and "the key
  // is being created" are results, and results are toasts. `needs_key` is a
  // live prerequisite: the credential exists but is not that person's default,
  // so nothing will pick it up, and the admin has one more thing to do. That
  // one stays on the page, as an alert carrying the action that discharges it.
  //
  // Read from the *server's* answer for this person (`api_key_onboarding_state`
  // — the same field their own paste-a-key wall reads), never inferred from
  // having created a row: `set_as_default` defaults to false, so a perfectly
  // good credential can leave its only member walled.
  const [keyNotice, setKeyNotice] = useState<{
    record: ManagedAICredentialPublic
    userId: string
    label: string
  } | null>(null)
  const [noticeEditOpen, setNoticeEditOpen] = useState(false)

  // Two surfaces that configure each other: a provider owns the managed
  // credential it grants through, so they live on one page rather than on two
  // pages an admin has to know to visit in order.
  //
  // `HashTabs` owns which tab is showing (it is URL-addressable, which is what
  // makes `/admin/ai-credentials#providers` linkable from the Access tab, the
  // invite wizard and the read-only credential dialog). This mirror exists for
  // the things rendered *outside* the `Tabs` tree: the page header's per-tab
  // button, and the Keys tab's poll, which must not run while the other tab is
  // showing.
  const [tab, setTab] = useState<TabValue>("keys")
  const handleTabChange = useCallback(
    (value: string) => setTab(value as TabValue),
    [],
  )

  // **One record, not the list.** The notice below is about a single grant, and
  // this page no longer loads every record to answer a question about one of
  // them — that whole-list read (every record with every member embedded) is
  // exactly what the Keys tab replaced. Enabled only while a notice is armed.
  const { data: liveNoticeRecord } = useQuery({
    queryKey: [
      ...MANAGED_CREDENTIALS_QUERY_PREFIX,
      "record",
      keyNotice?.record.id,
    ],
    queryFn: () =>
      AdminLlmProvidersService.getManagedAiCredential({
        managedCredentialId: keyNotice!.record.id,
      }),
    enabled: Boolean(keyNotice),
    staleTime: 30_000,
  })

  // The alert answers to the server, not to us: once this member's state stops
  // being `needs_key` — the admin set the default here, or the person chose it
  // themselves in Settings — the reason for the alert is gone and so is the
  // alert. Until the record arrives we fall back to the one the invite handed
  // us, so the notice is never blank while it loads.
  const noticeRecord = keyNotice ? (liveNoticeRecord ?? keyNotice.record) : null
  const noticeMember =
    keyNotice && noticeRecord
      ? noticeRecord.members?.find((m) => m.user_id === keyNotice.userId)
      : undefined
  // No member row left means the grant itself is gone — removed here or the
  // record deleted — and nothing is owed on it any more. Defaulting that case
  // to `needs_key` would pin an alert open that only Dismiss could clear.
  const noticeStillOwed = noticeMember?.api_key_onboarding_state === "needs_key"

  useEffect(() => {
    setHeaderContent(
      <>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">AI Credentials</h1>
          <p className="text-xs text-muted-foreground">
            Provision AI credentials on behalf of users
          </p>
        </div>
        <div className="flex items-center gap-2">
          {tab === "keys" ? (
            <ManagedCredentialDialog mode="create" />
          ) : (
            <ConnectProviderDialog />
          )}
        </div>
      </>,
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent, tab])

  const tabs: TabConfig[] = [
    {
      value: "keys",
      title: "Keys",
      content: <KeysTab active={tab === "keys"} />,
    },
    { value: "providers", title: "Providers", content: <ProvidersTab /> },
  ]

  // Edge-case guard for a non-superuser that slipped past beforeLoad.
  if (user && !user.is_superuser) {
    return (
      <div className="p-6 text-center text-muted-foreground">
        You do not have permission to view this page.
      </div>
    )
  }

  return (
    <div className="p-6 md:p-8 overflow-y-auto">
      <div className="mx-auto max-w-7xl space-y-4">
        {keyNotice && noticeStillOwed && (
          <Alert>
            <AlertTriangle />
            <AlertTitle>Key added — not their default</AlertTitle>
            <AlertDescription>
              <p>
                {keyNotice.label || "That account"} has a key on "
                {noticeRecord?.name ?? keyNotice.record.name}", but it is not
                set as their default, so nothing will pick it up yet. Set it as
                their default here — or they can choose it themselves in
                Settings.
              </p>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setNoticeEditOpen(true)}
                >
                  Open credential
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setKeyNotice(null)
                    // Both, always: a stuck `true` would pop the edit dialog
                    // unbidden the next time a notice appears.
                    setNoticeEditOpen(false)
                  }}
                >
                  Dismiss
                </Button>
              </div>
            </AlertDescription>
          </Alert>
        )}

        {keyNotice && noticeRecord && (
          <ManagedCredentialDialog
            mode="edit"
            record={noticeRecord}
            open={noticeEditOpen}
            onOpenChange={setNoticeEditOpen}
          />
        )}

        {/* Controlled, and mounted only while the hand-off is live: the page
            header already carries this dialog's own uncontrolled instance for
            an admin who came here to create a credential from scratch. */}
        {handoff && (
          <ManagedCredentialDialog
            mode="create"
            open
            onOpenChange={(open) => {
              if (!open) setHandoff(null)
            }}
            initialTargets={[
              {
                id: handoff.id,
                userId: handoff.id,
                // Keeps the preselected member from rendering as "Unknown
                // user" until the picker's own lookup resolves.
                fallbackLabel: handoff.label,
              },
            ]}
            nameSubject={handoff.label}
            onCreated={(created) => {
              const state =
                created.record.members?.find((m) => m.user_id === handoff.id)
                  ?.api_key_onboarding_state ?? "needs_key"
              if (state === "has_key") {
                showSuccessToast("Key added.")
                return
              }
              if (state === "preparing") {
                showSuccessToast("The key is being created now.")
                return
              }
              setKeyNotice({
                record: created.record,
                userId: handoff.id,
                label: handoff.label,
              })
            }}
          />
        )}

        <HashTabs tabs={tabs} onTabChange={handleTabChange} />
      </div>
    </div>
  )
}
