import { ChevronDown, ChevronRight } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"

export interface ArtifactDetailsValues {
  title: string
  description: string
  /**
   * "" = no start page. On upload / publish the server then uses a top-level
   * index.html when there is one; on edit (PATCH `entry_path: null`) the
   * artifact becomes a file set — hence the two labels below.
   */
  startPage: string
}

/** The Select's value for "no start page" (a Select item cannot be ""). */
const NO_START_PAGE = "__files__"

type StartPageMode =
  /** Upload / publish: the files are not known yet, so a text field under Advanced. */
  | { mode: "text" }
  /** Edit: a value picker over the revision's `.html` files. */
  | { mode: "select"; options: string[] }
  /** A single file: there is no start page to choose. */
  | { mode: "none" }

interface ArtifactDetailsFieldsProps {
  idPrefix: string
  values: ArtifactDetailsValues
  onChange: (next: ArtifactDetailsValues) => void
  disabled?: boolean
  startPage: StartPageMode
}

/**
 * Title, description and start page — the fields S3 (upload), S7 (edit) and
 * S10 (publish from a workspace) share, so the three cannot drift apart.
 */
export function ArtifactDetailsFields({
  idPrefix,
  values,
  onChange,
  disabled,
  startPage,
}: ArtifactDetailsFieldsProps) {
  // Starts closed on every open: `DialogContent` unmounts on close.
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const set = (patch: Partial<ArtifactDetailsValues>) =>
    onChange({ ...values, ...patch })

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <Label htmlFor={`${idPrefix}-title`}>Title</Label>
        <Input
          id={`${idPrefix}-title`}
          value={values.title}
          onChange={(e) => set({ title: e.target.value })}
          maxLength={200}
          required
          disabled={disabled}
        />
      </div>

      <div className="space-y-1.5">
        <Label htmlFor={`${idPrefix}-description`}>
          Description{" "}
          <span className="font-normal text-muted-foreground">(optional)</span>
        </Label>
        <Textarea
          id={`${idPrefix}-description`}
          rows={3}
          value={values.description}
          onChange={(e) => set({ description: e.target.value })}
          maxLength={2000}
          disabled={disabled}
        />
      </div>

      {startPage.mode === "select" ? (
        <div className="space-y-1.5">
          <Label htmlFor={`${idPrefix}-start`}>Start page</Label>
          <Select
            value={values.startPage || NO_START_PAGE}
            onValueChange={(v) =>
              set({ startPage: v === NO_START_PAGE ? "" : v })
            }
            disabled={disabled}
          >
            <SelectTrigger id={`${idPrefix}-start`} className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={NO_START_PAGE}>
                None — show the files
              </SelectItem>
              {startPage.options.map((path) => (
                <SelectItem key={path} value={path}>
                  <span className="font-mono text-xs">{path}</span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      ) : startPage.mode === "none" ? null : (
        <div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="px-0 text-muted-foreground"
            onClick={() => setAdvancedOpen((prev) => !prev)}
            aria-expanded={advancedOpen}
            disabled={disabled}
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
              <Label htmlFor={`${idPrefix}-start`}>Start page</Label>
              <Input
                id={`${idPrefix}-start`}
                value={values.startPage}
                onChange={(e) => set({ startPage: e.target.value })}
                placeholder="index.html"
                className="font-mono text-sm"
                disabled={disabled}
              />
              <p className="text-xs text-muted-foreground">
                Leave empty to use index.html at the top, or show the files.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
