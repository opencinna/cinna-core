import { AdvancedDisclosure } from "@/components/Common/AdvancedDisclosure"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { FieldError } from "./McpEntryConnectionFields"
import {
  type McpDraftErrors,
  type McpEntryDraft,
  slugify,
} from "./mcpEntryDraft"

interface McpEntryListingFieldsProps {
  draft: McpEntryDraft
  onChange: (patch: Partial<McpEntryDraft>) => void
  errors: McpDraftErrors
  idPrefix: string
  /**
   * `auto` — the name follows the title until the admin types in it (create);
   * `edited` — the admin has typed in it; `readonly` — the name is the
   * entry's slug and cannot change (edit).
   */
  nameMode: "auto" | "edited" | "readonly"
  onNameEdited?: () => void
  /**
   * `collapsed` — the optional fields sit behind one Advanced disclosure (the
   * wizard's step 3); `inline` — every field shows (the Edit Sheet).
   */
  optionalFields: "collapsed" | "inline"
  /** The name field's server-side error (409 `name_taken`). */
  nameError?: string
}

/**
 * What users read in the directory: title, name, description, and the
 * optional icon, categories, homepage, publisher and verified mark. The Add
 * wizard's step 3 and the Edit Sheet's Listing section.
 */
export function McpEntryListingFields({
  draft,
  onChange,
  errors,
  idPrefix,
  nameMode,
  onNameEdited,
  optionalFields,
  nameError,
}: McpEntryListingFieldsProps) {
  const id = (name: string) => `${idPrefix}-${name}`
  const nameMessage = nameError || errors.name

  const optional = (
    <div className="grid gap-4">
      <TextField
        id={id("icon")}
        maxLength={2048}
        label="Icon URL"
        value={draft.iconUrl}
        onChange={(iconUrl) => onChange({ iconUrl })}
        placeholder="https://example.com/icon.png"
        error={errors.iconUrl}
        mono
      />
      <TextField
        id={id("categories")}
        maxLength={2048}
        label="Categories"
        value={draft.categories}
        onChange={(categories) => onChange({ categories })}
        placeholder="Comma separated, e.g. productivity, notes"
        error={errors.categories}
      />
      <TextField
        id={id("homepage")}
        maxLength={2048}
        label="Homepage"
        value={draft.homepage}
        onChange={(homepage) => onChange({ homepage })}
        placeholder="https://example.com"
        error={errors.homepage}
        mono
      />
      <TextField
        id={id("publisher")}
        maxLength={255}
        label="Publisher"
        value={draft.publisher}
        onChange={(publisher) => onChange({ publisher })}
        placeholder="Who makes this server"
      />
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <Label htmlFor={id("verified")} className="font-normal">
            Verified publisher
          </Label>
          <p className="text-xs text-muted-foreground">
            Shows a verified mark next to the server's title.
          </p>
        </div>
        <Switch
          id={id("verified")}
          checked={draft.verified}
          onCheckedChange={(verified) => onChange({ verified })}
        />
      </div>
    </div>
  )

  return (
    <div className="grid gap-4">
      <TextField
        id={id("title")}
        maxLength={255}
        label="Title"
        value={draft.title}
        onChange={(title) =>
          onChange(
            nameMode === "auto" ? { title, name: slugify(title) } : { title },
          )
        }
        placeholder="Linear"
        error={errors.title}
      />
      <div className="grid gap-1.5">
        <Label htmlFor={id("name")}>Name</Label>
        <Input
          id={id("name")}
          maxLength={64}
          value={draft.name}
          readOnly={nameMode === "readonly"}
          onChange={(e) => {
            onNameEdited?.()
            onChange({ name: e.target.value.toLowerCase() })
          }}
          placeholder="linear"
          aria-invalid={nameMessage ? true : undefined}
          aria-describedby={id("name-help")}
          className="font-mono text-sm read-only:bg-muted/50 read-only:text-muted-foreground"
          spellCheck={false}
          autoComplete="off"
        />
        {nameMessage ? (
          <FieldError id={id("name-help")} message={nameMessage} />
        ) : (
          <p id={id("name-help")} className="text-xs text-muted-foreground">
            {nameMode === "readonly"
              ? "The server's identifier; it cannot be changed."
              : "The server's identifier in the directory; it cannot be changed later."}
          </p>
        )}
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor={id("description")}>Description</Label>
        <Textarea
          id={id("description")}
          maxLength={20000}
          value={draft.description}
          onChange={(e) => onChange({ description: e.target.value })}
          placeholder="What the server lets an agent do"
          rows={3}
        />
      </div>
      {optionalFields === "inline" ? (
        optional
      ) : (
        <AdvancedDisclosure
          // A refused field inside must not stay hidden behind the toggle.
          forceOpen={Boolean(
            errors.iconUrl || errors.categories || errors.homepage,
          )}
        >
          {optional}
        </AdvancedDisclosure>
      )}
    </div>
  )
}

function TextField({
  id,
  label,
  value,
  onChange,
  placeholder,
  error,
  mono,
  maxLength,
}: {
  id: string
  label: string
  value: string
  onChange: (value: string) => void
  placeholder?: string
  error?: string
  mono?: boolean
  maxLength: number
}) {
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : undefined}
        maxLength={maxLength}
        className={mono ? "font-mono text-sm" : undefined}
        spellCheck={mono ? false : undefined}
        autoComplete="off"
      />
      <FieldError id={`${id}-error`} message={error} />
    </div>
  )
}
