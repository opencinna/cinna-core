import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { useEffect, useMemo, useState } from "react"

import type { AddonPublic, PluginSyncResponse } from "@/client"
import { LlmPluginsService, SkillsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import {
  type AddAddonKind,
  type AddAddonResult,
  buildAddonResults,
  invalidateAddons,
  isAddonResultBlocked,
} from "@/utils/addons"
import { AddAddonChooseStep } from "./AddAddonChooseStep"
import { AddAddonModesStep } from "./AddAddonModesStep"
import type { SyncReporter } from "./AddonRow"

/** The server page this dialog asks for, and the line it prints at the edge. */
const RESULT_LIMIT = 50

type Step = "choose" | "modes"

interface AddAddonDialogProps {
  agentId: string
  /** What the agent already carries — left out of the results. */
  installedAddons: AddonPublic[]
  /** The projection failed, so nothing can be filtered out as installed. */
  addonsUnavailable: boolean
  addonsError: unknown
  onRetryAddons: () => void
  onSyncResult: SyncReporter
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * "Find a plugin or a skill by name and install it into this agent with the
 * right modes" — S3.
 *
 * **P6 with two steps**, not P4: the kind does *not* reshape the form. A plugin
 * and a catalog skill are installed by the same two booleans, so kind is a
 * filter over one result list and there is no `<Select type>` anywhere in here
 * (A7). Replaces both the Plugins tab's "Available plugins" discover grid and
 * `InstallPluginModal`.
 *
 * This component keeps the state, the two searches, the install mutation and
 * the stepper; each step's markup is its own file, and the fold that merges the
 * two sources is `buildAddonResults` in `utils/addons.ts`, which is pure.
 */
export function AddAddonDialog({
  agentId,
  installedAddons,
  addonsUnavailable,
  addonsError,
  onRetryAddons,
  onSyncResult,
  open,
  onOpenChange,
}: AddAddonDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast } = useCustomToast()

  const [step, setStep] = useState<Step>("choose")
  const [query, setQuery] = useState("")
  const [debouncedQuery, setDebouncedQuery] = useState("")
  const [kind, setKind] = useState<AddAddonKind>("all")
  // The chosen result is **held**, not looked up in the live result list: a
  // background refetch that dropped or renamed the row would otherwise blank
  // step 2 while leaving its Install button enabled.
  const [selected, setSelected] = useState<AddAddonResult | null>(null)
  const [conversationMode, setConversationMode] = useState(true)
  const [buildingMode, setBuildingMode] = useState(true)
  const [revisionNumber, setRevisionNumber] = useState("latest")

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query.trim()), 300)
    return () => clearTimeout(timer)
  }, [query])

  // Both queries run unconditionally: this component is mounted only while the
  // dialog is open, so an `enabled: open` would be a guard that is always true.
  const {
    data: pluginsData,
    isLoading: isLoadingPlugins,
    isError: isPluginsError,
    error: pluginsError,
    refetch: refetchPlugins,
  } = useQuery({
    queryKey: ["discover-plugins", debouncedQuery, RESULT_LIMIT],
    queryFn: () =>
      LlmPluginsService.discoverPlugins({
        search: debouncedQuery || undefined,
        limit: RESULT_LIMIT,
      }),
  })

  const {
    data: catalogData,
    isLoading: isLoadingCatalog,
    isError: isCatalogError,
    error: catalogError,
    refetch: refetchCatalog,
  } = useQuery({
    queryKey: ["skills-catalog"],
    queryFn: () => SkillsService.listSkillCatalog(),
  })

  const results = useMemo(
    () =>
      buildAddonResults({
        plugins: pluginsData?.data ?? [],
        packages: catalogData?.data ?? [],
        installedAddons,
        agentId,
        query: debouncedQuery,
        kind,
      }),
    [agentId, catalogData, debouncedQuery, installedAddons, kind, pluginsData],
  )

  // The cap bounds what you can *pick*, not what you can read. Sorting
  // unsupported rows to the bottom and then slicing the bottom off would
  // delete precisely the rows this dialog exists to keep visible — an
  // unsupported entry has to stay so an admin's half-broken marketplace does
  // not read as empty (plan §10). So the slice runs over the selectable rows
  // only and the blocked ones are appended whole; the fold already ordered
  // both groups, so concatenating preserves that order.
  const selectable = results.filter((result) => !isAddonResultBlocked(result))
  const blocked = results.filter(isAddonResultBlocked)
  const cappedResults = [...selectable.slice(0, RESULT_LIMIT), ...blocked]
  const isCapped = selectable.length > RESULT_LIMIT

  const settleSync = (result: PluginSyncResponse, name: string) => {
    if (
      (result.failed_syncs && result.failed_syncs > 0) ||
      result.partial_failures
    ) {
      // A partial failure is not a success with a footnote: it goes to the
      // tab's sync-issues dialog rather than to a toast that claims the install
      // worked everywhere.
      onSyncResult(`${name} installed`, result)
    } else {
      showSuccessToast(`${name} installed`)
    }
    invalidateAddons(queryClient, agentId, { catalog: true })
    onOpenChange(false)
  }

  const installMutation = useMutation({
    mutationFn: () => {
      if (!selected) throw new Error("Nothing selected")
      // The route is the *source*, never the word on the row: a
      // `skills`-format marketplace entry says "skill" and is still installed
      // as a marketplace plugin link, so branching on `kind` here would post a
      // marketplace plugin id to the catalog endpoint.
      if (selected.source === "marketplace") {
        return LlmPluginsService.installAgentPlugin({
          agentId,
          requestBody: {
            plugin_id: selected.id,
            conversation_mode: conversationMode,
            building_mode: buildingMode,
          },
        })
      }
      return SkillsService.installAgentSkill({
        agentId,
        requestBody: {
          package_id: selected.id,
          revision_number:
            revisionNumber === "latest" ? null : Number(revisionNumber),
          conversation_mode: conversationMode,
          building_mode: buildingMode,
        },
      })
    },
    onSuccess: (result) => settleSync(result, selected?.name ?? "The addon"),
    // No `onError`: both refusals are coded and belong in the dialog, where the
    // choice that caused them still is. A toast would take it off screen.
  })

  const isPending = installMutation.isPending
  // Discovery counts under every filter, the Skills segment included: a
  // `skills`-format marketplace entry is a skill, so a "Skills" list that
  // rendered as empty while discovery was still in flight would be claiming
  // there are none.
  const isLoadingResults =
    isLoadingPlugins || (kind !== "plugin" && isLoadingCatalog)
  // One source down is a degraded list, not a dead one; both down is a dead
  // one, and the two branches say which.
  //
  // "Down" is `isError` *and* nothing cached, never `isError` alone: a failed
  // background refetch leaves renderable data behind it, and gating on the
  // flag would blank a working list on a transient failure — or, worse, print
  // "showing catalog skills only" over plugin rows that are still on screen.
  const pluginsDown = isPluginsError && !pluginsData
  const catalogDown = isCatalogError && !catalogData
  const bothFailed = pluginsDown && catalogDown
  const halfFailedMessage =
    pluginsDown && !catalogDown
      ? "Couldn't search the plugin marketplaces — showing catalog skills only."
      : catalogDown && !pluginsDown
        ? "Couldn't read the skills catalog — showing marketplace plugins only."
        : null

  const retrySearch = () => {
    if (isPluginsError) refetchPlugins()
    if (isCatalogError) refetchCatalog()
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Escape and outside-click must not close a dialog mid-request.
        if (!isPending) onOpenChange(next)
      }}
    >
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          {/* The stepper P6 asks for: two panes with no stepper give no sense
              of how far along the user is, or that there is a second step. */}
          <p className="text-xs text-muted-foreground">
            <span
              className={
                step === "choose" ? "font-medium text-foreground" : undefined
              }
            >
              1 Choose
            </span>
            {" · "}
            <span
              className={
                step === "modes" ? "font-medium text-foreground" : undefined
              }
            >
              2 Modes
            </span>
          </p>
          {/* Step 2 is titled by the thing being installed — its name, linked
              to where it lives, its version — with its description under it.
              The modes are the body's question; the header says what they are
              being chosen for. */}
          {step === "choose" || !selected ? (
            <>
              <DialogTitle>Add addon</DialogTitle>
              <DialogDescription>
                Search the plugin marketplaces and the skills catalog together.
              </DialogDescription>
            </>
          ) : (
            <>
              <DialogTitle className="flex min-w-0 items-center gap-2">
                {selected.source === "catalog" ? (
                  <Link
                    to="/catalog/skills/$packageId"
                    params={{ packageId: selected.id }}
                    target="_blank"
                    className="truncate hover:underline"
                  >
                    {selected.name}
                  </Link>
                ) : selected.plugin?.repository_url ? (
                  <a
                    href={selected.plugin.repository_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="truncate hover:underline"
                  >
                    {selected.name}
                  </a>
                ) : (
                  <span className="truncate">{selected.name}</span>
                )}
                {selected.version && (
                  <Badge variant="secondary" className="h-5 shrink-0">
                    v{selected.version}
                  </Badge>
                )}
              </DialogTitle>
              <DialogDescription className="line-clamp-2">
                {selected.description ||
                  selected.origin ||
                  (selected.source === "catalog"
                    ? "From the skills catalog"
                    : "From a marketplace")}
              </DialogDescription>
            </>
          )}
        </DialogHeader>

        {step === "choose" ? (
          <AddAddonChooseStep
            query={query}
            onQueryChange={setQuery}
            debouncedQuery={debouncedQuery}
            kind={kind}
            onKindChange={setKind}
            results={cappedResults}
            isCapped={isCapped}
            resultLimit={RESULT_LIMIT}
            selectedKey={selected?.key ?? null}
            onSelect={(result) => {
              setSelected(result)
              // The revision belongs to the package that was chosen, so
              // choosing a different one must not carry it over — otherwise
              // step 2 can post a revision number the new package has never
              // heard of, with nothing on screen showing it.
              setRevisionNumber("latest")
              // So does the refusal: React Query holds `error` until the next
              // `mutate()`, so without this, a 409 on entry A survives Back →
              // pick B → Next and step 2 shows B's name above A's refusal,
              // naming a defect B does not have.
              installMutation.reset()
            }}
            isLoading={isLoadingResults}
            disabled={isPending}
            addonsUnavailable={addonsUnavailable}
            addonsError={addonsError}
            onRetryAddons={onRetryAddons}
            bothFailed={bothFailed}
            halfFailedMessage={halfFailedMessage}
            searchError={pluginsError ?? catalogError}
            onRetrySearch={retrySearch}
          />
        ) : (
          selected && (
            <AddAddonModesStep
              selected={selected}
              conversationMode={conversationMode}
              onConversationModeChange={setConversationMode}
              buildingMode={buildingMode}
              onBuildingModeChange={setBuildingMode}
              revisionNumber={revisionNumber}
              onRevisionNumberChange={setRevisionNumber}
              disabled={isPending}
              installError={installMutation.error}
              isInstallError={installMutation.isError}
            />
          )
        )}

        <DialogFooter>
          {step === "choose" ? (
            <>
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
              >
                Cancel
              </Button>
              <Button
                type="button"
                disabled={!selected}
                onClick={() => setStep("modes")}
              >
                Next
              </Button>
            </>
          ) : (
            <>
              <Button
                type="button"
                variant="outline"
                disabled={isPending}
                onClick={() => setStep("choose")}
              >
                Back
              </Button>
              <LoadingButton
                type="button"
                loading={isPending}
                disabled={!selected || (!conversationMode && !buildingMode)}
                onClick={() => installMutation.mutate()}
              >
                Install
              </LoadingButton>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
