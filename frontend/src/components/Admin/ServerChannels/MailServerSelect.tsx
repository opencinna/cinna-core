/**
 * Pick a mail server for an email channel's `incoming_server_id` /
 * `outgoing_server_id`.
 *
 * The stored value is a `mail_server_config` id, and those rows are managed on
 * the Mail servers tab of this same admin page — so a free-text UUID field
 * would ask an admin to copy an id out of a list that is one click away. The
 * list is filtered by `serverType` because the adapter refuses an IMAP id in
 * the SMTP slot at save time; offering only the right kind turns that refusal
 * into a choice that cannot be made.
 */
import { useQuery } from "@tanstack/react-query"
import type { ComponentProps } from "react"

import { MailServersService, type MailServerType } from "@/client"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { getErrorMessage } from "@/utils"

type TriggerProps = Omit<
  ComponentProps<typeof SelectTrigger>,
  "children" | "value" | "onChange"
>

interface Props extends TriggerProps {
  serverType: MailServerType
  /** The stored id, or `""` when nothing is chosen yet. */
  value: string
  onChange: (next: string) => void
}

const KIND_LABEL: Record<MailServerType, string> = {
  imap: "IMAP",
  smtp: "SMTP",
}

export function MailServerSelect({
  serverType,
  value,
  onChange,
  ...triggerProps
}: Props) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["mailServers", serverType],
    queryFn: () => MailServersService.listMailServers({ serverType }),
  })

  const servers = data?.data ?? []
  const kind = KIND_LABEL[serverType]

  // A stored id the list doesn't contain — the server was deleted, or renamed
  // into another type — must still render as *something*. Radix shows the
  // placeholder for a value with no matching item, which would read as "no
  // server configured" and invite the admin to pick one, silently replacing a
  // reference they never meant to touch.
  const isOrphaned = value !== "" && !servers.some((s) => s.id === value)

  return (
    <div className="space-y-1">
      {/* `value` stays controlled through the empty string — Radix renders the
          placeholder for a value no item matches, and swapping between
          undefined and a string would flip the control between uncontrolled
          and controlled mid-edit. */}
      <Select value={value} onValueChange={onChange} disabled={isLoading}>
        <SelectTrigger {...triggerProps}>
          <SelectValue
            placeholder={
              isLoading ? "Loading mail servers…" : `Choose a ${kind} server…`
            }
          />
        </SelectTrigger>
        <SelectContent>
          {servers.map((server) => (
            <SelectItem key={server.id} value={server.id}>
              {server.name}{" "}
              <span className="text-muted-foreground">
                ({server.host}:{server.port})
              </span>
            </SelectItem>
          ))}
          {isOrphaned && (
            <SelectItem value={value}>Unknown server ({value})</SelectItem>
          )}
        </SelectContent>
      </Select>

      {/* A failed fetch must not look like "no mail servers exist" — that
          sends the admin off to create a duplicate of one they already have. */}
      {isError ? (
        <p className="text-xs text-destructive">
          {getErrorMessage(error, "Couldn't load mail servers.")} The stored
          value is unchanged.
        </p>
      ) : isOrphaned ? (
        <p className="text-xs text-destructive">
          This channel points at a {kind} server that no longer exists. Pick
          another one, or re-create it on the Mail servers tab.
        </p>
      ) : !isLoading && servers.length === 0 ? (
        <p className="text-xs text-amber-600 dark:text-amber-400">
          No {kind} servers configured yet. Add one on the Mail servers tab,
          then come back.
        </p>
      ) : null}
    </div>
  )
}
