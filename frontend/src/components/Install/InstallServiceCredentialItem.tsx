/**
 * InstallServiceCredentialItem — one accordion entry per service spec.
 *
 * Three flavours:
 *   - PBP (provided_by="publisher"): collapsed by default, labelled "no
 *     action needed". The radio block is hidden.
 *   - PBU (provided_by="user"): radio choices for "Use my existing X"
 *     (when an auto-prefill suggestion exists), "Skip — set up later"
 *     (the placeholder default when no suggestion), and "Pick another
 *     credential…" (opens a dropdown of the user's matching-type
 *     credentials).
 *   - PBT (provided_by="template"): same radio shape as PBU plus a
 *     "Create from publisher template" option (the default when there is
 *     no suggestion — it materialises a fresh placeholder seeded with
 *     the publisher's non-private values).
 *
 * The "expanded by default" state:
 *   - PBP: collapsed.
 *   - PBU/PBT with a suggestion: collapsed (auto-prefill).
 *   - PBU/PBT without a suggestion: expanded.
 */
import { useQuery } from "@tanstack/react-query"
import { CheckCircle2, ChevronDown, ChevronRight } from "lucide-react"
import { useState } from "react"

import {
  CredentialsService,
  type InstallContextSpec,
} from "@/client"
import { Badge } from "@/components/ui/badge"
import { CredentialTypeBadge } from "@/components/Credentials/CredentialTypeBadge"
import { Label } from "@/components/ui/label"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

export type ServiceCredentialChoice =
  | { mode: "use_suggested" }
  | { mode: "use_existing"; credential_id: string }
  | { mode: "skip" } // resolves to placeholder server-side (or template materialisation for PBT)
  | { mode: "publisher_provides" }

interface InstallServiceCredentialItemProps {
  spec: InstallContextSpec
  choice: ServiceCredentialChoice
  onChange: (next: ServiceCredentialChoice) => void
}

const RADIO_USE_SUGGESTED = "use_suggested"
const RADIO_SKIP = "skip"
const RADIO_PICK_OTHER = "pick_other"
const RADIO_TEMPLATE = "template"

export function InstallServiceCredentialItem({
  spec,
  choice,
  onChange,
}: InstallServiceCredentialItemProps) {
  const isPublisher = spec.provided_by === "publisher"
  const isTemplate = spec.provided_by === "template"
  const hasSuggestion = Boolean(spec.suggested_credential_id)

  // Default-expand only PBU specs that have no auto-prefill suggestion
  // (matches plan §3 layout rules). Publisher and template specs both
  // start collapsed because the install creates the credential for them
  // — the user only needs to fill private fields after install.
  const [expanded, setExpanded] = useState<boolean>(
    !isPublisher && !isTemplate && !hasSuggestion,
  )

  const privateFieldCount = isTemplate
    ? (spec.template_private_fields ?? []).length
    : 0

  // Collapsed-state summary — one short line per item.
  const summary = (() => {
    if (isPublisher) {
      const s = spec.publisher_summary
      return s ? `Shared by publisher (${s.name})` : "Shared by publisher"
    }
    if (choice.mode === "use_suggested" && spec.suggested_credential_name) {
      return `Will use "${spec.suggested_credential_name}"`
    }
    if (choice.mode === "use_existing") {
      return "Custom credential picked"
    }
    if (isTemplate) {
      if (privateFieldCount > 0) {
        return `Create from template — fill in ${privateFieldCount} private field${privateFieldCount === 1 ? "" : "s"} after install`
      }
      return "Create from template — no private fields"
    }
    return "Skip — set up later"
  })()

  return (
    <div className="rounded-md border bg-background">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-start gap-2 px-3 py-2 text-left hover:bg-muted/40"
      >
        <span className="mt-0.5 shrink-0 text-muted-foreground">
          {expanded ? (
            <ChevronDown className="h-4 w-4" />
          ) : (
            <ChevronRight className="h-4 w-4" />
          )}
        </span>
        <div className="flex-1 min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <CredentialTypeBadge type={spec.type} />
            {isPublisher ? (
              <Badge
                variant="outline"
                className="text-xs font-normal gap-1 border-emerald-300 text-emerald-700"
              >
                <CheckCircle2 className="h-3 w-3" />
                publisher-provided
              </Badge>
            ) : isTemplate ? (
              <Badge
                variant="outline"
                className="text-xs font-normal gap-1 border-sky-300 text-sky-700"
              >
                <CheckCircle2 className="h-3 w-3" />
                template
              </Badge>
            ) : (
              <Badge variant="outline" className="text-xs font-normal">
                user-provided
              </Badge>
            )}
          </div>
          <div className="text-xs text-muted-foreground flex items-center gap-2 flex-wrap">
            <Badge variant="outline" className="font-normal">
              {spec.name}
            </Badge>
            <span className="truncate">{summary}</span>
          </div>
        </div>
      </button>

      {expanded && (
        <div className="px-3 pb-3 pt-1 border-t bg-muted/20">
          {spec.description && (
            <p className="text-xs text-muted-foreground mb-3">
              {spec.description}
            </p>
          )}
          {isPublisher ? (
            <PublisherProvidedBody spec={spec} />
          ) : (
            <UserOrTemplateChoicesBody
              spec={spec}
              choice={choice}
              onChange={onChange}
              isTemplate={isTemplate}
            />
          )}
        </div>
      )}
    </div>
  )
}

function PublisherProvidedBody({ spec }: { spec: InstallContextSpec }) {
  const summary = spec.publisher_summary
  return (
    <div className="text-sm space-y-1">
      <p>
        <CheckCircle2 className="inline h-4 w-4 text-emerald-500 mr-1.5 align-text-bottom" />
        Shared by the publisher — no action needed.
      </p>
      {summary && (
        <p className="text-xs text-muted-foreground">
          Linked to <span className="font-medium">{summary.name}</span>{" "}
          ({summary.type})
        </p>
      )}
    </div>
  )
}

function UserOrTemplateChoicesBody({
  spec,
  choice,
  onChange,
  isTemplate,
}: {
  spec: InstallContextSpec
  choice: ServiceCredentialChoice
  onChange: (next: ServiceCredentialChoice) => void
  isTemplate: boolean
}) {
  const { data: credentialsData } = useQuery({
    queryKey: ["credentials"],
    queryFn: () => CredentialsService.readCredentials(),
  })
  const matchingCredentials = (credentialsData?.data ?? []).filter((c) => {
    const credType =
      typeof c.type === "string" ? c.type : (c.type as { value?: string }).value
    return credType === spec.type
  })

  // "skip" maps to a placeholder for PBU and to template materialisation
  // for PBT — both flow through the same backend payload mode, but the
  // radio value identifier is split so the UI labels stay distinct.
  const fallbackRadioValue = isTemplate ? RADIO_TEMPLATE : RADIO_SKIP

  const radioValue = (() => {
    if (choice.mode === "use_suggested") return RADIO_USE_SUGGESTED
    if (choice.mode === "use_existing") return RADIO_PICK_OTHER
    return fallbackRadioValue
  })()

  const handleRadioChange = (value: string) => {
    if (value === RADIO_USE_SUGGESTED) {
      onChange({ mode: "use_suggested" })
      return
    }
    if (value === RADIO_SKIP || value === RADIO_TEMPLATE) {
      onChange({ mode: "skip" })
      return
    }
    // RADIO_PICK_OTHER — pre-fill with the first matching credential when
    // we don't have a current selection, otherwise keep the existing pick.
    if (choice.mode === "use_existing") {
      onChange(choice)
      return
    }
    if (matchingCredentials.length > 0) {
      onChange({
        mode: "use_existing",
        credential_id: matchingCredentials[0].id,
      })
      return
    }
    // No matching credential to pick — fall back to the placeholder/template
    // path so the install can still proceed.
    onChange({ mode: "skip" })
  }

  const dropdownDisabled =
    radioValue !== RADIO_PICK_OTHER || matchingCredentials.length === 0

  const privateFields = spec.template_private_fields ?? []

  return (
    <div className="space-y-3">
      <RadioGroup value={radioValue} onValueChange={handleRadioChange}>
        {spec.suggested_credential_id && (
          <div className="flex items-start gap-2">
            <RadioGroupItem
              id={`${spec.name}-use-suggested`}
              value={RADIO_USE_SUGGESTED}
              className="mt-0.5"
            />
            <div className="flex-1 min-w-0">
              <Label
                htmlFor={`${spec.name}-use-suggested`}
                className="cursor-pointer"
              >
                Use my{" "}
                <span className="font-medium">
                  "{spec.suggested_credential_name}"
                </span>
              </Label>
              <p className="text-xs text-muted-foreground">
                Auto-detected by type {isTemplate ? "(reuses your existing setup)" : "and name"}. You can change this below.
              </p>
            </div>
          </div>
        )}

        <div className="flex items-start gap-2">
          <RadioGroupItem
            id={`${spec.name}-${fallbackRadioValue}`}
            value={fallbackRadioValue}
            className="mt-0.5"
          />
          <div className="flex-1 min-w-0">
            <Label
              htmlFor={`${spec.name}-${fallbackRadioValue}`}
              className="cursor-pointer"
            >
              {isTemplate
                ? "Create from publisher template"
                : "Skip — set up later"}
            </Label>
            {isTemplate ? (
              <p className="text-xs text-muted-foreground">
                A new credential is created from the publisher's defaults
                {privateFields.length > 0
                  ? `. You'll need to fill in ${privateFields.length} private field${privateFields.length === 1 ? "" : "s"} after install.`
                  : "."}
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                A placeholder is created. Fill it in from the agent settings
                when you're ready.
              </p>
            )}
            {isTemplate && privateFields.length > 0 && (
              <ul className="mt-1.5 list-disc pl-5 space-y-0.5 text-xs text-muted-foreground">
                {privateFields.map((f) => (
                  <li key={f} className="font-mono">
                    {f}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <div className="flex items-start gap-2">
          <RadioGroupItem
            id={`${spec.name}-pick-other`}
            value={RADIO_PICK_OTHER}
            className="mt-0.5"
          />
          <div className="flex-1 min-w-0">
            <Label
              htmlFor={`${spec.name}-pick-other`}
              className="cursor-pointer"
            >
              {isTemplate
                ? "Use one of my existing credentials"
                : "Pick another credential"}
            </Label>
            <div className="mt-1.5">
              <Select
                value={
                  choice.mode === "use_existing" ? choice.credential_id : ""
                }
                onValueChange={(value) =>
                  onChange({ mode: "use_existing", credential_id: value })
                }
                disabled={dropdownDisabled}
              >
                <SelectTrigger className="h-8 text-sm">
                  <SelectValue
                    placeholder={
                      matchingCredentials.length === 0
                        ? `No ${spec.type} credentials yet`
                        : "Choose a credential"
                    }
                  />
                </SelectTrigger>
                <SelectContent>
                  {matchingCredentials.map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        </div>
      </RadioGroup>
    </div>
  )
}
