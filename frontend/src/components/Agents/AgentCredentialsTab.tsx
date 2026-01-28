import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Plus, ExternalLink, Unlink, Users } from "lucide-react"
import { useState, useMemo } from "react"

import { AgentsService, CredentialsService } from "@/client"
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import useWorkspace from "@/hooks/useWorkspace"
import { handleError } from "@/utils"

interface AgentCredentialsTabProps {
  agentId: string
}

function getCredentialTypeLabel(type: string): string {
  switch (type) {
    case "email_imap":
      return "Email (IMAP)"
    case "odoo":
      return "Odoo"
    case "gmail_oauth":
      return "Gmail OAuth"
    case "gmail_oauth_readonly":
      return "Gmail OAuth (Read-Only)"
    case "gdrive_oauth":
      return "Google Drive OAuth"
    case "gdrive_oauth_readonly":
      return "Google Drive OAuth (Read-Only)"
    case "gcalendar_oauth":
      return "Google Calendar OAuth"
    case "gcalendar_oauth_readonly":
      return "Google Calendar OAuth (Read-Only)"
    case "google_service_account":
      return "Google Service Account"
    case "api_token":
      return "API Token"
    default:
      return type
  }
}

export function AgentCredentialsTab({ agentId }: AgentCredentialsTabProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { activeWorkspaceId } = useWorkspace()
  const [isAddDialogOpen, setIsAddDialogOpen] = useState(false)
  const [selectedCredentialId, setSelectedCredentialId] = useState<
    string | undefined
  >(undefined)

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
    queryKey: ["credentials", activeWorkspaceId],
    queryFn: ({ queryKey }) => {
      const [, workspaceId] = queryKey
      return CredentialsService.readCredentials({
        skip: 0,
        limit: 100,
        userWorkspaceId: workspaceId ?? "",
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
          <Dialog open={isAddDialogOpen} onOpenChange={setIsAddDialogOpen}>
            <DialogTrigger asChild>
              <Button size="sm">
                <Plus className="mr-2 h-4 w-4" />
                Add Credential
              </Button>
            </DialogTrigger>
            <DialogContent>
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
                  <Select
                    value={selectedCredentialId}
                    onValueChange={setSelectedCredentialId}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="Select a credential" />
                    </SelectTrigger>
                    <SelectContent>
                      {availableCredentials.map((credential) => (
                        <SelectItem key={credential.id} value={credential.id}>
                          <div className="flex items-center gap-2">
                            <span>{credential.name} ({getCredentialTypeLabel(credential.type)})</span>
                            {credential.isSharedWithMe && (
                              <span className="text-xs text-blue-600 flex items-center gap-1">
                                <Users className="h-3 w-3" />
                                from {credential.ownerEmail}
                              </span>
                            )}
                          </div>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </div>
              <DialogFooter>
                <Button
                  variant="outline"
                  onClick={() => {
                    setIsAddDialogOpen(false)
                    setSelectedCredentialId(undefined)
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
      </CardHeader>
      <CardContent>
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
              {agentCredentials.map((credential) => (
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
                      {getCredentialTypeLabel(credential.type)}
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
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}
