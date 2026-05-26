import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { Network, Plus, Search } from "lucide-react"
import { useMemo, useState } from "react"

import {
  CredentialsService,
  type CredentialCreate,
  type CredentialType,
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
import {
  CREDENTIAL_TYPE_GROUPS,
  type CredentialTypeOption,
} from "@/components/Credentials/credentialTypes"
import { ConnectAgentApiDialog } from "@/components/Credentials/ConnectAgentApiDialog"

type CredentialTypeKey = CredentialType

const SSH_KEY_DEFAULT_DATA = {
  mode: "generate" as const,
  key_type: "ed25519" as const,
}

const AddCredential = () => {
  const [isOpen, setIsOpen] = useState(false)
  const [query, setQuery] = useState("")
  const [pendingType, setPendingType] = useState<CredentialTypeKey | null>(null)
  const [connectOpen, setConnectOpen] = useState(false)
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { workspaceFilter } = useWorkspace()

  const filteredGroups = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return CREDENTIAL_TYPE_GROUPS
    return CREDENTIAL_TYPE_GROUPS.map((group) => ({
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
    <>
    <ConnectAgentApiDialog open={connectOpen} onOpenChange={setConnectOpen} />
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

        <div className="px-6 pb-3 space-y-3">
          <button
            type="button"
            onClick={() => {
              setIsOpen(false)
              setConnectOpen(true)
            }}
            className="flex w-full items-center gap-2 rounded-md border border-dashed px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground"
          >
            <Network className="h-4 w-4 shrink-0" />
            <span>Connect Agent API</span>
            <span className="ml-auto text-xs text-muted-foreground">
              another agent's REST API
            </span>
          </button>
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
    </>
  )
}

export default AddCredential
