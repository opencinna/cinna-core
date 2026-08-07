import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import {
  AlertTriangle,
  ExternalLink,
  Network,
  Plug,
  Plus,
  Search,
  Unlink,
  Users,
} from "lucide-react"
import { useState, useMemo } from "react"

import { ConnectAgentApiDialog } from "@/components/Credentials/ConnectAgentApiDialog"
import { ConnectMcpProviderDialog } from "@/components/Credentials/ConnectMcpProviderDialog"

import {
  AgentsService,
  CredentialsService,
  type CredentialType,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  CREDENTIAL_TYPE_GROUPS,
  getCredentialTypeMeta,
} from "@/components/Credentials/credentialTypes"
import useCustomToast from "@/hooks/useCustomToast"
import useWorkspace from "@/hooks/useWorkspace"
import { cn } from "@/lib/utils"
import { handleError } from "@/utils"

interface AgentCredentialsTabProps {
  agentId: string
}

export function AgentCredentialsTab({ agentId }: AgentCredentialsTabProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { workspaceFilter } = useWorkspace()
  const [isAddDialogOpen, setIsAddDialogOpen] = useState(false)
  const [isConnectOpen, setIsConnectOpen] = useState(false)
  const [isConnectMcpOpen, setIsConnectMcpOpen] = useState(false)
  const [selectedCredentialId, setSelectedCredentialId] = useState<
    string | undefined
  >(undefined)
  const [credentialQuery, setCredentialQuery] = useState("")

  // Fetch agent credentials
  const {
    data: agentCredentialsData,
    isLoading: isLoadingAgentCredentials,
    error: agentCredentialsError,
  } = useQuery({
    queryKey: ["agent-credentials", agentId],
    queryFn: () => AgentsService.readAgentCredentials({ id: agentId }),
  })

  // Fetch user's own credentials for the add dialog
  const { data: ownedCredentialsData } = useQuery({
    queryKey: ["credentials", workspaceFilter],
    queryFn: ({ queryKey }) => {
      const [, workspaceId] = queryKey
      return CredentialsService.readCredentials({
        skip: 0,
        limit: 100,
        userWorkspaceId: workspaceId as string | undefined,
      })
    },
    enabled: isAddDialogOpen,
  })

  // Fetch credentials shared with the user
  const { data: sharedCredentialsData } = useQuery({
    queryKey: ["credentials-shared-with-me"],
    queryFn: () => CredentialsService.getCredentialsSharedWithMe(),
    enabled: isAddDialogOpen,
  })

  const agentCredentials = agentCredentialsData?.data || []
  const ownedCredentials = ownedCredentialsData?.data || []
  const sharedCredentials = sharedCredentialsData?.data || []

  const incompleteCredentials = useMemo(
    () =>
      agentCredentials.filter(
        (cred) => cred.is_placeholder || cred.status === "incomplete",
      ),
    [agentCredentials],
  )

  // Combine owned and shared credentials, marking which are shared
  const allCredentials = useMemo(() => {
    const owned = ownedCredentials.map((cred) => ({
      ...cred,
      isSharedWithMe: false,
      ownerEmail: undefined as string | undefined,
    }))
    const shared = sharedCredentials.map((cred) => ({
      id: cred.id,
      name: cred.name,
      type: cred.type,
      notes: cred.notes,
      isSharedWithMe: true,
      ownerEmail: cred.owner_email,
    }))
    return [...owned, ...shared]
  }, [ownedCredentials, sharedCredentials])

  // Filter out credentials that are already linked
  const availableCredentials = allCredentials.filter(
    (cred) => !agentCredentials.some((ac) => ac.id === cred.id)
  )

  type AvailableCredential = (typeof availableCredentials)[number]

  // Apply the search filter (by credential name and type label) before grouping
  const filteredCredentials = useMemo(() => {
    const q = credentialQuery.trim().toLowerCase()
    if (!q) return availableCredentials
    return availableCredentials.filter((cred) => {
      const typeLabel = getCredentialTypeMeta(cred.type).label
      return `${cred.name} ${typeLabel}`.toLowerCase().includes(q)
    })
  }, [availableCredentials, credentialQuery])

  // Group the filtered credentials by their type's registry group. Any
  // credential whose type is not part of a registered group (all current
  // types are, including agent_api's "API & Access" group) is collected into
  // a trailing "Other" group as a fallback so nothing is silently dropped if
  // a future type is added without a group.
  const groupedCredentials = useMemo(() => {
    const assigned = new Set<string>()
    const groups: { key: string; label: string; items: AvailableCredential[] }[] =
      []

    for (const group of CREDENTIAL_TYPE_GROUPS) {
      const groupTypes = new Set(group.options.map((opt) => opt.type))
      const items = filteredCredentials.filter((cred) => {
        if (!groupTypes.has(cred.type as CredentialType)) return false
        assigned.add(cred.id)
        return true
      })
      if (items.length > 0) {
        groups.push({ key: group.key, label: group.label, items })
      }
    }

    const other = filteredCredentials.filter((cred) => !assigned.has(cred.id))
    if (other.length > 0) {
      groups.push({ key: "__other", label: "Other", items: other })
    }

    return groups
  }, [filteredCredentials])

  // Add credential mutation
  const addMutation = useMutation({
    mutationFn: (credentialId: string) =>
      AgentsService.addCredentialToAgent({
        id: agentId,
        requestBody: { credential_id: credentialId },
      }),
    onSuccess: () => {
      showSuccessToast("Credential added successfully")
      setIsAddDialogOpen(false)
      setSelectedCredentialId(undefined)
      setCredentialQuery("")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-credentials", agentId] })
    },
  })

  // Remove credential mutation
  const removeMutation = useMutation({
    mutationFn: (credentialId: string) =>
      AgentsService.removeCredentialFromAgent({
        id: agentId,
        credentialId: credentialId,
      }),
    onSuccess: () => {
      showSuccessToast("Credential removed successfully")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-credentials", agentId] })
    },
  })

  const handleAdd = () => {
    if (selectedCredentialId) {
      addMutation.mutate(selectedCredentialId)
    }
  }

  const handleRemove = (credentialId: string) => {
    removeMutation.mutate(credentialId)
  }

  if (isLoadingAgentCredentials) {
    return (
      <Card>
        <CardContent className="pt-6">
          <p className="text-muted-foreground">Loading credentials...</p>
        </CardContent>
      </Card>
    )
  }

  if (agentCredentialsError) {
    return (
      <Card>
        <CardContent className="pt-6">
          <p className="text-destructive">
            Error loading credentials: {(agentCredentialsError as Error).message}
          </p>
        </CardContent>
      </Card>
    )
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle>Shared Credentials</CardTitle>
            <CardDescription>
              Manage credentials that this agent can access.
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => setIsConnectOpen(true)}
          >
            <Network className="mr-2 h-4 w-4" />
            Connect Agent API
          </Button>
          <ConnectAgentApiDialog
            open={isConnectOpen}
            onOpenChange={setIsConnectOpen}
            defaultConsumerAgentId={agentId}
            onConnected={() =>
              queryClient.invalidateQueries({
                queryKey: ["agent-credentials", agentId],
              })
            }
          />
          <Button
            size="sm"
            variant="outline"
            onClick={() => setIsConnectMcpOpen(true)}
          >
            <Plug className="mr-2 h-4 w-4" />
            Connect MCP Provider
          </Button>
          <ConnectMcpProviderDialog
            open={isConnectMcpOpen}
            onOpenChange={setIsConnectMcpOpen}
            defaultConsumerAgentId={agentId}
            onConnected={() =>
              queryClient.invalidateQueries({
                queryKey: ["agent-credentials", agentId],
              })
            }
          />
          <Dialog
            open={isAddDialogOpen}
            onOpenChange={(open) => {
              setIsAddDialogOpen(open)
              if (!open) {
                setSelectedCredentialId(undefined)
                setCredentialQuery("")
              }
            }}
          >
            <DialogTrigger asChild>
              <Button size="sm">
                <Plus className="mr-2 h-4 w-4" />
                Add Credential
              </Button>
            </DialogTrigger>
            <DialogContent className="sm:max-w-xl">
              <DialogHeader>
                <DialogTitle>Add Credential to Agent</DialogTitle>
                <DialogDescription>
                  Select a credential to share with this agent.
                </DialogDescription>
              </DialogHeader>
              <div className="py-4">
                {availableCredentials.length === 0 ? (
                  <p className="text-sm text-muted-foreground">
                    No available credentials to add. All your credentials are
                    already shared with this agent, or you haven't created any
                    credentials yet.
                  </p>
                ) : (
                  <div className="space-y-3">
                    <div className="relative">
                      <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
                      <Input
                        autoFocus
                        placeholder="Search credentials…"
                        value={credentialQuery}
                        onChange={(e) => setCredentialQuery(e.target.value)}
                        className="pl-9"
                      />
                    </div>
                    <div className="max-h-[50vh] overflow-y-auto">
                      {groupedCredentials.length === 0 ? (
                        <p className="py-6 text-center text-sm text-muted-foreground">
                          No credentials match "{credentialQuery}"
                        </p>
                      ) : (
                        <div className="space-y-4">
                          {groupedCredentials.map((group) => (
                            <div key={group.key}>
                              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                                {group.label}
                              </div>
                              <div className="flex flex-wrap gap-2">
                                {group.items.map((credential) => {
                                  const meta = getCredentialTypeMeta(
                                    credential.type,
                                  )
                                  const Icon = meta.icon
                                  const isSelected =
                                    credential.id === selectedCredentialId
                                  return (
                                    <button
                                      key={credential.id}
                                      type="button"
                                      onClick={() =>
                                        setSelectedCredentialId(credential.id)
                                      }
                                      title={
                                        credential.isSharedWithMe &&
                                        credential.ownerEmail
                                          ? `Shared by ${credential.ownerEmail}`
                                          : undefined
                                      }
                                      className={cn(
                                        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm font-medium transition-colors",
                                        meta.badgeClass,
                                        isSelected &&
                                          "ring-2 ring-current ring-offset-2 ring-offset-background",
                                      )}
                                    >
                                      <Icon className="h-3.5 w-3.5 shrink-0" />
                                      <span>{credential.name}</span>
                                      {credential.isSharedWithMe && (
                                        <span className="ml-1 inline-flex items-center gap-1 text-xs opacity-70">
                                          <Users className="h-3 w-3 shrink-0" />
                                          <span className="max-w-[12ch] truncate">
                                            from {credential.ownerEmail}
                                          </span>
                                        </span>
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
                  </div>
                )}
              </div>
              <DialogFooter>
                <Button
                  variant="outline"
                  onClick={() => {
                    setIsAddDialogOpen(false)
                    setSelectedCredentialId(undefined)
                    setCredentialQuery("")
                  }}
                >
                  Cancel
                </Button>
                <LoadingButton
                  onClick={handleAdd}
                  loading={addMutation.isPending}
                  disabled={!selectedCredentialId}
                >
                  Add
                </LoadingButton>
              </DialogFooter>
            </DialogContent>
          </Dialog>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {incompleteCredentials.length > 0 && (
          <Alert className="mb-4 border-amber-500/50 bg-amber-50 text-amber-900 dark:border-amber-400/40 dark:bg-amber-950/40 dark:text-amber-100 [&>svg]:text-amber-600 dark:[&>svg]:text-amber-300">
            <AlertTriangle className="h-4 w-4" />
            <AlertDescription>
              {incompleteCredentials.length === 1
                ? "1 credential still needs to be filled in. "
                : `${incompleteCredentials.length} credentials still need to be filled in. `}
              Click a credential below to open its form and add the missing
              details.
            </AlertDescription>
          </Alert>
        )}
        {agentCredentials.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <p className="text-muted-foreground mb-4">
              No credentials have been shared with this agent yet.
            </p>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setIsAddDialogOpen(true)}
            >
              <Plus className="mr-2 h-4 w-4" />
              Add Your First Credential
            </Button>
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Notes</TableHead>
                <TableHead className="w-[100px]">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {agentCredentials.map((credential) => {
                const needsSetup =
                  credential.is_placeholder ||
                  credential.status === "incomplete"
                return (
                <TableRow key={credential.id}>
                  <TableCell className="font-medium">
                    <div className="flex items-center gap-2">
                      <Link
                        to="/credential/$credentialId"
                        params={{ credentialId: credential.id }}
                        className="inline-flex items-center gap-1 hover:text-primary transition-colors"
                      >
                        {credential.name}
                        <ExternalLink className="h-3 w-3" />
                      </Link>
                      {needsSetup && (
                        <Badge
                          variant="outline"
                          className="text-xs border-amber-500/60 bg-amber-50 text-amber-800 dark:bg-amber-950/40 dark:text-amber-100 dark:border-amber-400/40"
                          title={
                            credential.is_placeholder
                              ? "This credential is a placeholder — open it to fill in the required values."
                              : "This credential is missing required fields — open it to complete the form."
                          }
                        >
                          <AlertTriangle className="h-3 w-3 mr-1" />
                          Setup needed
                        </Badge>
                      )}
                      {credential.is_shared && (
                        <Badge variant="outline" className="text-xs bg-blue-50 text-blue-700 border-blue-200">
                          <Users className="h-3 w-3 mr-1" />
                          Shared
                        </Badge>
                      )}
                    </div>
                    {credential.is_shared && credential.owner_email && (
                      <span className="text-xs text-muted-foreground">
                        from {credential.owner_email}
                      </span>
                    )}
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary">
                      {getCredentialTypeMeta(credential.type).label}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {credential.notes && (
                      <span className="text-sm text-muted-foreground">
                        {credential.notes}
                      </span>
                    )}
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => handleRemove(credential.id)}
                      disabled={removeMutation.isPending}
                      title="Unshare"
                    >
                      <Unlink className="h-4 w-4 text-destructive" />
                      <span className="sr-only">Remove credential</span>
                    </Button>
                  </TableCell>
                </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}
