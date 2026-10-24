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
import { REGISTRY_OPTIONS, REMOTE_AUTH_OPTIONS } from "./mcpDirectoryCopy"
import type {
  McpDraftErrors,
  McpEntryDraft,
  McpRegistry,
} from "./mcpEntryDraft"

interface McpEntryConnectionFieldsProps {
  draft: McpEntryDraft
  onChange: (patch: Partial<McpEntryDraft>) => void
  errors: McpDraftErrors
  idPrefix: string
}

export function FieldError({ id, message }: { id: string; message?: string }) {
  if (!message) return null
  return (
    <p id={id} className="text-xs text-destructive">
      {message}
    </p>
  )
}

/**
 * How the server is reached, for the transport already chosen: URL + Auth for
 * a remote server, Registry · Package · Version · Arguments for a local
 * process. The Add wizard's step 2 and the Edit Sheet's Connection section.
 * Auth for stdio is derived from the environment list and not asked.
 */
export function McpEntryConnectionFields({
  draft,
  onChange,
  errors,
  idPrefix,
}: McpEntryConnectionFieldsProps) {
  const id = (name: string) => `${idPrefix}-${name}`

  if (draft.transport === "http" || draft.transport === "sse") {
    // An imported or older entry may carry an auth this form does not offer
    // for a remote server; it stays selectable rather than silently changing.
    const authOptions = REMOTE_AUTH_OPTIONS.some((o) => o.value === draft.auth)
      ? REMOTE_AUTH_OPTIONS
      : [...REMOTE_AUTH_OPTIONS, { value: draft.auth, label: "Environment" }]
    return (
      <div className="grid gap-4">
        <div className="grid gap-1.5">
          <Label htmlFor={id("url")}>URL</Label>
          <Input
            id={id("url")}
            maxLength={2048}
            value={draft.url}
            onChange={(e) => onChange({ url: e.target.value })}
            placeholder="https://mcp.example.com/mcp"
            aria-invalid={errors.url ? true : undefined}
            aria-describedby={errors.url ? id("url-error") : undefined}
            className="font-mono text-sm"
            spellCheck={false}
            autoComplete="off"
          />
          <FieldError id={id("url-error")} message={errors.url} />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor={id("auth")}>Auth</Label>
          <Select
            value={draft.auth}
            onValueChange={(value) =>
              onChange({ auth: value as McpEntryDraft["auth"] })
            }
          >
            <SelectTrigger id={id("auth")} className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {authOptions.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
    )
  }

  if (draft.transport === "stdio") {
    return (
      <div className="grid gap-4">
        <div className="grid grid-cols-[8rem_minmax(0,1fr)] gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor={id("registry")}>Registry</Label>
            <Select
              value={draft.registry}
              onValueChange={(value) =>
                onChange({ registry: value as McpRegistry })
              }
            >
              <SelectTrigger id={id("registry")} className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {REGISTRY_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor={id("package")}>Package</Label>
            <Input
              id={id("package")}
              value={draft.packageName}
              onChange={(e) => onChange({ packageName: e.target.value })}
              placeholder="@scope/server-name"
              aria-invalid={errors.packageName ? true : undefined}
              aria-describedby={
                errors.packageName ? id("package-error") : undefined
              }
              className="font-mono text-sm"
              spellCheck={false}
              autoComplete="off"
            />
            <FieldError id={id("package-error")} message={errors.packageName} />
          </div>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor={id("version")}>Version</Label>
          <Input
            id={id("version")}
            value={draft.version}
            onChange={(e) => onChange({ version: e.target.value })}
            placeholder="latest"
            className="font-mono text-sm"
            spellCheck={false}
            autoComplete="off"
          />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor={id("args")}>Arguments</Label>
          <Textarea
            id={id("args")}
            value={draft.args}
            onChange={(e) => onChange({ args: e.target.value })}
            placeholder={"One per line, e.g.\n--read-only"}
            rows={3}
            className="font-mono text-sm"
            spellCheck={false}
          />
        </div>
      </div>
    )
  }

  return null
}
