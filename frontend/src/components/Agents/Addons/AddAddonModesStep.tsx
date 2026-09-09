import { useQuery } from "@tanstack/react-query"
import {
  ChevronDown,
  ChevronRight,
  Info,
  MessageCircle,
  Wrench,
} from "lucide-react"
import { useState } from "react"

import { SkillsService } from "@/client"
import { SkillCatalogErrorAlert } from "@/components/Catalog/SkillCatalogErrorAlert"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import type { AddAddonResult } from "@/utils/addons"
import { skillRevisionLabel } from "@/utils/skillCatalog"

interface AddAddonModesStepProps {
  /** The choice from step 1, held in the dialog's state — not re-derived. */
  selected: AddAddonResult
  conversationMode: boolean
  onConversationModeChange: (next: boolean) => void
  buildingMode: boolean
  onBuildingModeChange: (next: boolean) => void
  /** `"latest"`, or a revision number as a string. */
  revisionNumber: string
  onRevisionNumberChange: (next: string) => void
  disabled: boolean
  installError: unknown
  isInstallError: boolean
}

/**
 * Step 2 of the Add addon wizard: the two booleans, and nothing else.
 *
 * This step is why the dialog is P6 rather than P4: a marketplace entry and a
 * catalog skill are installed by the *same* two modes, so neither the kind nor
 * the source reshapes the form. The only source-specific control is the
 * revision picker, which lives behind the one "Advanced" disclosure §1 allows
 * and appears only for a catalog package that actually has a choice to make.
 */
export function AddAddonModesStep({
  selected,
  conversationMode,
  onConversationModeChange,
  buildingMode,
  onBuildingModeChange,
  revisionNumber,
  onRevisionNumberChange,
  disabled,
  installError,
  isInstallError,
}: AddAddonModesStepProps) {
  const [advancedOpen, setAdvancedOpen] = useState(false)

  // The revision list lives only on the package detail payload, and only a
  // *catalog* package has one to choose from. Gated on the source, not on the
  // word: a `skills`-format marketplace entry reads as a skill and has no
  // catalog package behind it, so keying this on `kind` would ask the catalog
  // for a marketplace plugin id and 404.
  const isCatalogPackage = selected.source === "catalog"
  const { data: pkg } = useQuery({
    queryKey: ["skills-catalog", "package", selected.id],
    queryFn: () => SkillsService.getSkillPackage({ packageId: selected.id }),
    enabled: isCatalogPackage,
  })
  const revisions = pkg?.revisions ?? []
  const latestLabel = pkg?.latest_revision
    ? skillRevisionLabel(pkg.latest_revision)
    : null

  return (
    <div className="space-y-4">
      <div className="space-y-3">
        <Label className="text-sm font-medium">Enable for:</Label>
        <div className="flex items-start space-x-3">
          <Checkbox
            id="add-addon-conversation"
            checked={conversationMode}
            disabled={disabled}
            onCheckedChange={(checked) =>
              onConversationModeChange(checked === true)
            }
          />
          <Label
            htmlFor="add-addon-conversation"
            className="flex cursor-pointer items-center gap-2 font-normal"
          >
            <MessageCircle className="h-4 w-4 text-muted-foreground" />
            Conversation mode
          </Label>
        </div>
        <div className="flex items-start space-x-3">
          <Checkbox
            id="add-addon-building"
            checked={buildingMode}
            disabled={disabled}
            onCheckedChange={(checked) =>
              onBuildingModeChange(checked === true)
            }
          />
          <Label
            htmlFor="add-addon-building"
            className="flex cursor-pointer items-center gap-2 font-normal"
          >
            <Wrench className="h-4 w-4 text-muted-foreground" />
            Building mode
          </Label>
        </div>
        {!conversationMode && !buildingMode && (
          <p className="text-xs text-destructive">
            At least one mode must be enabled
          </p>
        )}
        {/* The one rule of thumb the two boxes need: every addon the engine
            loads costs the conversation context, so a development-only one
            should not ride along on every chat turn. */}
        <Alert>
          <Info />
          <AlertDescription>
            Enable a plugin that is only meant for developing the agent, not for
            running it, in building mode alone. Keeping it out of conversation
            mode improves that mode's performance and the quality of its
            answers.
          </AlertDescription>
        </Alert>
      </div>

      {isCatalogPackage && revisions.length > 1 && (
        <div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="px-0 text-muted-foreground"
            onClick={() => setAdvancedOpen((prev) => !prev)}
            aria-expanded={advancedOpen}
          >
            {advancedOpen ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
            Advanced
          </Button>
          {advancedOpen && (
            <div className="space-y-1.5 pt-2">
              <Label htmlFor="add-addon-revision">Revision</Label>
              <Select
                value={revisionNumber}
                onValueChange={onRevisionNumberChange}
                disabled={disabled}
              >
                <SelectTrigger id="add-addon-revision" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="latest">
                    {latestLabel ? `Latest (${latestLabel})` : "Latest"}
                  </SelectItem>
                  {revisions.map((rev) => (
                    <SelectItem
                      key={rev.id}
                      value={String(rev.revision_number)}
                    >
                      {skillRevisionLabel(rev)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
        </div>
      )}

      {isInstallError && (
        <SkillCatalogErrorAlert
          error={installError}
          fallback="Couldn't install it"
        />
      )}
    </div>
  )
}
