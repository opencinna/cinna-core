import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import {
  Briefcase,
  Calendar,
  HardDrive,
  Inbox,
  Key,
  KeyRound,
  Mail,
  Plus,
  Search,
  Send,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react"
import { useMemo, useState } from "react"

import {
  CredentialsService,
  type CredentialCreate,
} from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import useCustomToast from "@/hooks/useCustomToast"
import useWorkspace from "@/hooks/useWorkspace"
import { cn } from "@/lib/utils"
import { handleError } from "@/utils"

type CredentialTypeKey =
  | "email_imap"
  | "email_smtp"
  | "gmail_oauth"
  | "gmail_oauth_readonly"
  | "gdrive_oauth"
  | "gdrive_oauth_readonly"
  | "gcalendar_oauth"
  | "gcalendar_oauth_readonly"
  | "google_service_account"
  | "api_token"
  | "ssh_key"
  | "odoo"

interface CredentialTypeOption {
  type: CredentialTypeKey
  label: string
  defaultName: string
  keywords: string
  icon: LucideIcon
}

interface CredentialTypeGroup {
  key: string
  label: string
  // Tailwind classes applied to every badge in this group. Kept as a single
  // concatenated string so the whole palette (bg + text + border + hover) lives
  // in one place per group.
  badgeClass: string
  options: CredentialTypeOption[]
}

const CREDENTIAL_GROUPS: CredentialTypeGroup[] = [
  {
    key: "api_access",
    label: "API & Access",
    badgeClass:
      "bg-slate-100 text-slate-800 border-slate-200 hover:bg-slate-200 dark:bg-slate-800/60 dark:text-slate-100 dark:border-slate-700 dark:hover:bg-slate-700",
    options: [
      {
        type: "api_token",
        label: "API Token",
        defaultName: "API Token",
        keywords: "api token bearer key secret",
        icon: Key,
      },
      {
        type: "ssh_key",
        label: "SSH Key",
        defaultName: "SSH Key",
        keywords: "ssh key git deploy private public",
        icon: KeyRound,
      },
    ],
  },
  {
    key: "email",
    label: "Email",
    badgeClass:
      "bg-amber-50 text-amber-900 border-amber-200 hover:bg-amber-100 dark:bg-amber-950/40 dark:text-amber-100 dark:border-amber-900 dark:hover:bg-amber-900/40",
    options: [
      {
        type: "email_imap",
        label: "Email (IMAP)",
        defaultName: "Email (IMAP)",
        keywords: "email imap mail inbox",
        icon: Inbox,
      },
      {
        type: "email_smtp",
        label: "Email (SMTP)",
        defaultName: "Email (SMTP)",
        keywords: "email smtp mail send outgoing",
        icon: Send,
      },
    ],
  },
  {
    key: "google",
    label: "Google",
    badgeClass:
      "bg-blue-50 text-blue-900 border-blue-200 hover:bg-blue-100 dark:bg-blue-950/40 dark:text-blue-100 dark:border-blue-900 dark:hover:bg-blue-900/40",
    options: [
      {
        type: "gmail_oauth",
        label: "Gmail",
        defaultName: "Gmail",
        keywords: "gmail google oauth mail",
        icon: Mail,
      },
      {
        type: "gmail_oauth_readonly",
        label: "Gmail (Read-Only)",
        defaultName: "Gmail (Read-Only)",
        keywords: "gmail google oauth readonly mail",
        icon: Mail,
      },
      {
        type: "gdrive_oauth",
        label: "Google Drive",
        defaultName: "Google Drive",
        keywords: "google drive files oauth",
        icon: HardDrive,
      },
      {
        type: "gdrive_oauth_readonly",
        label: "Google Drive (Read-Only)",
        defaultName: "Google Drive (Read-Only)",
        keywords: "google drive files oauth readonly",
        icon: HardDrive,
      },
      {
        type: "gcalendar_oauth",
        label: "Google Calendar",
        defaultName: "Google Calendar",
        keywords: "google calendar events oauth",
        icon: Calendar,
      },
      {
        type: "gcalendar_oauth_readonly",
        label: "Google Calendar (Read-Only)",
        defaultName: "Google Calendar (Read-Only)",
        keywords: "google calendar events oauth readonly",
        icon: Calendar,
      },
      {
        type: "google_service_account",
        label: "Google Service Account",
        defaultName: "Google Service Account",
        keywords: "google service account json sa",
        icon: ShieldCheck,
      },
    ],
  },
  {
    key: "applications",
    label: "Applications",
    badgeClass:
      "bg-violet-50 text-violet-900 border-violet-200 hover:bg-violet-100 dark:bg-violet-950/40 dark:text-violet-100 dark:border-violet-900 dark:hover:bg-violet-900/40",
    options: [
      {
        type: "odoo",
        label: "Odoo",
        defaultName: "Odoo",
        keywords: "odoo erp applications",
        icon: Briefcase,
      },
    ],
  },
]

const SSH_KEY_DEFAULT_DATA = {
  mode: "generate" as const,
  key_type: "ed25519" as const,
}

const AddCredential = () => {
  const [isOpen, setIsOpen] = useState(false)
  const [query, setQuery] = useState("")
  const [pendingType, setPendingType] = useState<CredentialTypeKey | null>(null)
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { workspaceFilter } = useWorkspace()

  const filteredGroups = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return CREDENTIAL_GROUPS
    return CREDENTIAL_GROUPS.map((group) => ({
      ...group,
      options: group.options.filter((opt) => {
        const haystack = `${opt.label} ${opt.keywords} ${group.label}`.toLowerCase()
        return haystack.includes(q)
      }),
    })).filter((group) => group.options.length > 0)
  }, [query])

  const createMutation = useMutation({
    mutationFn: (payload: CredentialCreate) =>
      CredentialsService.createCredential({ requestBody: payload }),
    onSuccess: (credential) => {
      showSuccessToast("Credential created — configure it below")
      handleClose()
      navigate({
        to: "/credential/$credentialId",
        params: { credentialId: credential.id },
        search: { new: 1 },
      })
    },
    onError: (err) => {
      setPendingType(null)
      handleError.bind(showErrorToast)(err as any)
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
    },
  })

  const handleSelect = (option: CredentialTypeOption) => {
    if (createMutation.isPending) return
    setPendingType(option.type)

    const payload: CredentialCreate = {
      name: option.defaultName,
      type: option.type,
      user_workspace_id: workspaceFilter || undefined,
    }
    // ssh_key requires credential_data at creation — default to a generated
    // ed25519 key. The user sees the resulting public key on the detail page
    // and can rotate / re-import later.
    if (option.type === "ssh_key") {
      payload.credential_data = { ...SSH_KEY_DEFAULT_DATA }
    }

    createMutation.mutate(payload)
  }

  const handleClose = () => {
    setIsOpen(false)
    setQuery("")
    setPendingType(null)
  }

  const handleOpenChange = (open: boolean) => {
    if (!open) {
      handleClose()
    } else {
      setIsOpen(true)
    }
  }

  return (
    <Dialog open={isOpen} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button className="my-4">
          <Plus className="mr-2" />
          Add Credential
        </Button>
      </DialogTrigger>

      <DialogContent className="sm:max-w-2xl p-0 gap-0 overflow-hidden">
        <DialogHeader className="px-6 pt-6 pb-3">
          <DialogTitle>Add Credential</DialogTitle>
          <DialogDescription>
            Pick a credential type. We'll create it with a default name so you
            can fill in the details on the next page.
          </DialogDescription>
        </DialogHeader>

        <div className="px-6 pb-3">
          <div className="relative">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input
              autoFocus
              placeholder="Search credential types…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="pl-9"
            />
          </div>
        </div>

        <div className="max-h-[60vh] overflow-y-auto px-6 pb-6">
          {filteredGroups.length === 0 ? (
            <p className="text-sm text-muted-foreground py-6 text-center">
              No credential types match "{query}"
            </p>
          ) : (
            <div className="space-y-4">
              {filteredGroups.map((group) => (
                <div key={group.key}>
                  <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-2">
                    {group.label}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {group.options.map((option) => {
                      const isPending =
                        createMutation.isPending && pendingType === option.type
                      const Icon = option.icon
                      return (
                        <button
                          key={option.type}
                          type="button"
                          disabled={createMutation.isPending}
                          onClick={() => handleSelect(option)}
                          className={cn(
                            "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm font-medium transition-colors",
                            group.badgeClass,
                            "disabled:opacity-50 disabled:cursor-not-allowed",
                            isPending && "ring-2 ring-offset-1 ring-current/40",
                          )}
                        >
                          <Icon className="h-3.5 w-3.5 shrink-0" />
                          <span>{option.label}</span>
                          {isPending && (
                            <span className="ml-1 inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
                          )}
                        </button>
                      )
                    })}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}

export default AddCredential
