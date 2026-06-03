/**
 * CredentialTemplateSharing — toggles "Share as Template" and lets the
 * publisher pick which credential_data fields are private.
 *
 * Sharing model recap:
 *   - "Allow Sharing"          → full credential is shared with the
 *     installer (publisher-provides). Recipients use the credential
 *     without ever seeing the values.
 *   - "Allow Template Sharing" → only the *non-private* fields are
 *     copied into the bundle revision as defaults; each installer gets
 *     their own credential row pre-filled with those defaults and a
 *     placeholder flag for the private fields they must supply.
 *
 * Designed to live next to <CredentialSharing /> on the credential
 * detail page; it's hidden for ``agent-user`` accounts (same gating as
 * full sharing).
 */
import { useMutation, useQuery } from "@tanstack/react-query"
import { useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { AlertTriangle, Box, Files } from "lucide-react"
import { useEffect, useState } from "react"

import { CredentialsService } from "@/client"
import type { CredentialWithData } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import useCustomToast from "@/hooks/useCustomToast"
import useRole from "@/hooks/useRole"
import { handleError } from "@/utils"

interface CredentialTemplateSharingProps {
  credential: CredentialWithData
}

// Credential types whose ``credential_data`` is dynamic per-user (OAuth
// tokens, service account JSON). For these the field list isn't useful —
// the publisher's data can never be reused, only ``notes`` carry through
// as setup instructions. The backend (PublishService._TEMPLATE_FORCE_PRIVATE_TYPES)
// also hard-strips credential_data on publish for these types as a
// defence-in-depth safeguard.
const FORCE_PRIVATE_TYPES = new Set<string>([
  "gmail_oauth",
  "gmail_oauth_readonly",
  "gdrive_oauth",
  "gdrive_oauth_readonly",
  "gcalendar_oauth",
  "gcalendar_oauth_readonly",
  "google_service_account",
])

// Known credential_data field schemas per type. Source of truth — mirrors
// the backend's CredentialsService.AGENT_ENV_ALLOWED_FIELDS. Listing
// fields here (instead of inferring from the stored credential_data)
// makes the UI consistent regardless of which fields the publisher has
// actually filled in yet.
//
// For ssh_key only ``host_aliases`` is shown because the rest is either
// per-key generated material (public_key / fingerprint / key_type) or a
// secret that never leaves the publisher (private_key / passphrase). The
// backend's PublishService._TEMPLATE_TEMPLATABLE_FIELDS_BY_TYPE enforces
// this allowlist on the publish side.
const FIELDS_BY_TYPE: Record<string, string[]> = {
  email_imap: ["host", "port", "login", "password", "is_ssl"],
  email_smtp: [
    "host",
    "port",
    "username",
    "password",
    "from_email",
    "use_tls",
    "use_ssl",
  ],
  odoo: ["url", "database_name", "login", "api_token"],
  api_token: ["api_token_type", "api_token_template", "api_token"],
  ssh_key: ["host_aliases"],
}

// Default-private fields per type — pre-checked the first time the
// publisher enables template sharing. The publisher can still uncheck
// any of them.
const DEFAULT_PRIVATE_FIELDS_BY_TYPE: Record<string, string[]> = {
  email_imap: ["login", "password"],
  email_smtp: ["username", "password"],
  odoo: ["login", "api_token"],
  api_token: ["api_token"],
}

// Human-readable labels mirrored from the per-type form fields under
// components/Credentials/CredentialFields/. Used in the private-field
// list so the publisher sees the same wording they used while filling
// in the credential.
const FIELD_LABELS_BY_TYPE: Record<string, Record<string, string>> = {
  email_imap: {
    host: "Host",
    port: "Port",
    login: "Login",
    password: "Password",
    is_ssl: "Use SSL",
  },
  email_smtp: {
    host: "Host",
    port: "Port",
    username: "Username",
    password: "Password",
    from_email: "From Email",
    use_tls: "Use TLS (STARTTLS)",
    use_ssl: "Use SSL",
  },
  odoo: {
    url: "URL",
    database_name: "Database Name",
    login: "Login",
    api_token: "API Token",
  },
  api_token: {
    api_token_type: "API Token Type",
    api_token_template: "API Token Template",
    api_token: "API Token",
    service_uri: "Service URI",
  },
  ssh_key: {
    host_aliases: "Host Aliases",
  },
}

// ``service_uri`` is a top-level Credential column (a non-secret slot id),
// NOT a ``credential_data`` field, so it is deliberately kept out of
// FIELDS_BY_TYPE (which mirrors the credential_data source of truth). It is
// appended as an extra toggleable row only for credential types where the
// slot id is meaningful. The toggle persists through the same
// ``template_private_fields`` mechanism — when present, the backend leaves
// service_uri blank on the installer's row (installer provides); when absent
// it copies the publisher's value as a shared default.
const SERVICE_URI_TYPES = new Set<string>(["api_token", "agent_api"])

function labelForField(type: string, field: string): string {
  // service_uri is appended as an extra row for SERVICE_URI_TYPES and may
  // not have a per-type label entry (e.g. agent_api) — give it a stable,
  // human-readable label everywhere.
  if (field === "service_uri") return "Service URI"
  return FIELD_LABELS_BY_TYPE[type]?.[field] ?? field
}

function getFieldsForType(
  type: string,
  data: Record<string, unknown> | null | undefined,
): string[] {
  // Prefer the static schema — guarantees consistent ordering plus all
  // fields appear even when the publisher hasn't filled them in yet.
  const known = FIELDS_BY_TYPE[type]
  if (known) return known
  // Fallback: discover from saved data for unregistered types.
  if (!data || typeof data !== "object") return []
  return Object.keys(data).filter((key) => key !== "")
}

export function CredentialTemplateSharing({
  credential,
}: CredentialTemplateSharingProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { isAgentUser } = useRole()

  const [allowTemplate, setAllowTemplate] = useState(
    credential.allow_template_sharing ?? false,
  )
  const [privateFields, setPrivateFields] = useState<string[]>(
    credential.template_private_fields ?? [],
  )

  useEffect(() => {
    setAllowTemplate(credential.allow_template_sharing ?? false)
    setPrivateFields(credential.template_private_fields ?? [])
  }, [credential.allow_template_sharing, credential.template_private_fields])

  const isForcePrivateType = FORCE_PRIVATE_TYPES.has(credential.type)

  const fieldNames = getFieldsForType(
    credential.type,
    credential.credential_data as Record<string, unknown> | undefined,
  )

  // Field rows the publisher can toggle private/shared. This is the
  // credential_data fields plus the top-level ``service_uri`` slot id for
  // types where it applies. service_uri defaults to shared (it is not in
  // DEFAULT_PRIVATE_FIELDS_BY_TYPE) and persists through the same
  // template_private_fields list as any other field name.
  const displayFieldNames = SERVICE_URI_TYPES.has(credential.type)
    ? [...fieldNames, "service_uri"]
    : fieldNames

  // Bundles whose publisher install has this credential linked AND
  // resolved as ``provided_by="template"``. Filtered client-side so the
  // section only renders when this credential ships as a template
  // somewhere.
  const { data: bundleUsages } = useQuery({
    queryKey: ["credential-bundle-usages", credential.id],
    queryFn: () =>
      CredentialsService.listCredentialBundleUsages({ id: credential.id }),
  })
  const templateUsages = (bundleUsages?.data ?? []).filter(
    (u) => u.provided_by === "template",
  )

  const updateMutation = useMutation({
    mutationFn: (payload: {
      allow_template_sharing?: boolean
      template_private_fields?: string[]
    }) =>
      CredentialsService.updateCredential({
        id: credential.id,
        requestBody: payload,
      }),
    onSuccess: () => {
      showSuccessToast("Template sharing settings updated")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({ queryKey: ["credential", credential.id] })
      queryClient.invalidateQueries({
        queryKey: ["credential-with-data", credential.id],
      })
    },
  })

  if (isAgentUser) {
    return null
  }

  const persist = (next: {
    allow_template_sharing?: boolean
    template_private_fields?: string[]
  }) => {
    updateMutation.mutate(next)
  }

  const handleToggle = (checked: boolean) => {
    setAllowTemplate(checked)
    let nextPrivate = privateFields
    if (checked && privateFields.length === 0 && fieldNames.length > 0) {
      const defaults = DEFAULT_PRIVATE_FIELDS_BY_TYPE[credential.type] ?? []
      nextPrivate = defaults.filter((f) => fieldNames.includes(f))
      if (nextPrivate.length === 0) {
        // Fall back to "no defaults" — the publisher will choose
        // explicitly. We do NOT auto-mark every field as private; that
        // would defeat the purpose of templating.
      }
      setPrivateFields(nextPrivate)
    }
    persist({
      allow_template_sharing: checked,
      template_private_fields: nextPrivate,
    })
  }

  const togglePrivateField = (field: string, isPrivate: boolean) => {
    const next = isPrivate
      ? [...privateFields.filter((f) => f !== field), field]
      : privateFields.filter((f) => f !== field)
    setPrivateFields(next)
    persist({ template_private_fields: next })
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2">
              <Files className="h-5 w-5" />
              Share as Template
            </CardTitle>
            <CardDescription>
              {allowTemplate
                ? "Bundles that need this credential will ship its non-private fields as defaults; users fill in the private ones."
                : "Enable to publish this credential as a template on bundles that require it."}
            </CardDescription>
          </div>
          <label className="flex cursor-pointer select-none items-center ml-4 mt-1">
            <div className="relative">
              <input
                type="checkbox"
                checked={allowTemplate}
                onChange={(e) => handleToggle(e.target.checked)}
                disabled={updateMutation.isPending}
                className="sr-only"
              />
              <div
                className={`block h-6 w-11 rounded-full transition-colors ${
                  allowTemplate ? "bg-emerald-500" : "bg-gray-300 dark:bg-gray-600"
                }`}
              />
              <div
                className={`dot absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                  allowTemplate ? "translate-x-5" : ""
                }`}
              />
            </div>
          </label>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {allowTemplate && isForcePrivateType && (
          <p className="text-sm text-muted-foreground">
            This credential type is per-user (each user authenticates
            themselves), so no field values are shared. Only the
            credential's <span className="font-medium">name</span> and{" "}
            <span className="font-medium">notes</span> are shipped with
            the bundle as setup instructions for users.
          </p>
        )}
        {allowTemplate && !isForcePrivateType && (
          <div className="space-y-3">
            <div>
              <h4 className="text-sm font-medium">Private fields</h4>
              <p className="text-xs text-muted-foreground">
                Check each field that contains a secret. Private fields
                are NEVER shipped with the bundle — every user must
                supply their own value. Unchecked fields are sent as
                template defaults.
              </p>
            </div>
            {displayFieldNames.length > 0 && privateFields.length === 0 && (
              <div className="rounded-md border border-amber-300 bg-amber-50 dark:bg-amber-950/30 dark:border-amber-900 p-3 text-xs text-amber-800 dark:text-amber-200 flex items-start gap-2">
                <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
                <div>
                  No private fields selected — every field listed below
                  will be shipped as a template default. Mark any field
                  that contains a secret as private.
                </div>
              </div>
            )}
            {displayFieldNames.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No fields detected on this credential yet. Save the
                credential first, then come back to choose which fields
                are private.
              </p>
            ) : (
              <ul className="space-y-2">
                {displayFieldNames.map((field) => {
                  const isPrivate = privateFields.includes(field)
                  return (
                    <li
                      key={field}
                      className="grid grid-cols-[auto_1fr_auto] items-center gap-3"
                    >
                      <Checkbox
                        id={`tpl-private-${field}`}
                        checked={isPrivate}
                        onCheckedChange={(checked) =>
                          togglePrivateField(field, !!checked)
                        }
                        disabled={updateMutation.isPending}
                      />
                      <Label
                        htmlFor={`tpl-private-${field}`}
                        className="text-sm truncate"
                      >
                        {labelForField(credential.type, field)}
                      </Label>
                      <span className="text-xs text-muted-foreground whitespace-nowrap text-left">
                        {isPrivate
                          ? "private - user has to provide"
                          : "shared - will be copied"}
                      </span>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        )}

        {templateUsages.length > 0 && (
          <div className="space-y-2 pt-2">
            <h4 className="text-sm font-medium">Used in Bundles</h4>
            <p className="text-xs text-muted-foreground">
              Bundles that ship this credential as a template — non-private
              fields are copied as defaults; users supply the private ones.
            </p>
            <ul className="space-y-1.5">
              {templateUsages.map((usage) => (
                <li
                  key={usage.bundle_uuid}
                  className="flex items-center justify-between gap-3 rounded-md border bg-muted/30 px-3 py-2"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <Box className="h-4 w-4 text-muted-foreground shrink-0" />
                    <div className="min-w-0">
                      <div className="text-sm font-medium truncate">
                        {usage.display_name}
                      </div>
                      <div className="text-xs text-muted-foreground truncate font-mono">
                        {usage.bundle_id}
                      </div>
                    </div>
                  </div>
                  {usage.publisher_install_id && (
                    <Button asChild variant="outline" size="sm">
                      <Link
                        to="/agent/$agentId"
                        params={{
                          agentId: usage.publisher_install_id,
                        }}
                        hash="bundle"
                      >
                        Open
                      </Link>
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

