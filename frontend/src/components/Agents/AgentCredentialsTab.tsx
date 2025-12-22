import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Plus, Trash2 } from "lucide-react"
import { useState } from "react"

import { AgentsService, CredentialsService } from "@/client"
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
import { handleError } from "@/utils"

interface AgentCredentialsTabProps {
  agentId: string
}

export function AgentCredentialsTab({ agentId }: AgentCredentialsTabProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
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

  // Fetch all user credentials for the add dialog
  const { data: allCredentialsData } = useQuery({
    queryKey: ["credentials"],
    queryFn: () => CredentialsService.readCredentials({ skip: 0, limit: 100 }),
    enabled: isAddDialogOpen,
  })

  const agentCredentials = agentCredentialsData?.data || []
  const allCredentials = allCredentialsData?.data || []

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
                          {credential.name} ({credential.type})
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
                    {credential.name}
                  </TableCell>
                  <TableCell>{credential.type}</TableCell>
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
                    >
                      <Trash2 className="h-4 w-4 text-destructive" />
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
