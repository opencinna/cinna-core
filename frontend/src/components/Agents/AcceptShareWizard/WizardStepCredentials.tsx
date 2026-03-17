import { useState, useMemo } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { CredentialsService, type PendingSharePublic, type CredentialCreate, type CredentialPublic } from "@/client"
import { CheckCircle, AlertCircle, Plus, Loader2 } from "lucide-react"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

// Credential type to display label mapping
const CREDENTIAL_TYPE_LABELS: Record<string, string> = {
  email_imap: "Email (IMAP)",
  email_smtp: "Email (SMTP)",
  odoo: "Odoo",
  gmail_oauth: "Gmail OAuth",
  gmail_oauth_readonly: "Gmail OAuth (Read-Only)",
  gdrive_oauth: "Google Drive OAuth",
  gdrive_oauth_readonly: "Google Drive OAuth (Read-Only)",
  gcalendar_oauth: "Google Calendar OAuth",
  gcalendar_oauth_readonly: "Google Calendar OAuth (Read-Only)",
  api_token: "API Token",
}

// Aggregated info about a credential type requirement
interface TypeRequirement {
  type: string
  hasShareable: boolean      // At least one shareable credential of this type
  nonShareableCount: number  // Number of non-shareable credentials of this type
  // Internal tracking - credential names for backend (not shown to user)
  shareableCredNames: string[]
  nonShareableCredNames: string[]
}

export interface CredentialSelection {
  sourceCredentialName: string
  sourceCredentialType: string
  allowSharing: boolean
  selectedCredentialId: string | null // null means "use shared" for shareable, or "not set" for non-shareable
}

interface WizardStepCredentialsProps {
  share: PendingSharePublic
  credentialSelections: Record<string, CredentialSelection>
  onChange: (selections: Record<string, CredentialSelection>) => void
  onNext: () => void
  onBack: () => void
}

export function WizardStepCredentials({
  share,
  credentialSelections,
  onChange,
  onNext,
  onBack,
}: WizardStepCredentialsProps) {
  const credentials = share.credentials_required || []
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [creatingForType, setCreatingForType] = useState<string | null>(null)
  const [creatingForIndex, setCreatingForIndex] = useState<number | null>(null)
  const [newCredentialName, setNewCredentialName] = useState("")
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Fetch user's credentials
  const { data: userCredentials, isLoading: isLoadingCredentials } = useQuery({
    queryKey: ["credentials"],
    queryFn: () => CredentialsService.readCredentials({ limit: 1000 }),
  })

  // Aggregate credentials by type
  const typeRequirements = useMemo((): TypeRequirement[] => {
    const typeMap: Record<string, TypeRequirement> = {}

    for (const cred of credentials) {
      if (!typeMap[cred.type]) {
        typeMap[cred.type] = {
          type: cred.type,
          hasShareable: false,
          nonShareableCount: 0,
          shareableCredNames: [],
          nonShareableCredNames: [],
        }
      }

      if (cred.allow_sharing) {
        typeMap[cred.type].hasShareable = true
        typeMap[cred.type].shareableCredNames.push(cred.name)
      } else {
        typeMap[cred.type].nonShareableCount++
        typeMap[cred.type].nonShareableCredNames.push(cred.name)
      }
    }

    return Object.values(typeMap)
  }, [credentials])

  // Get user's credentials filtered by type
  const getUserCredentialsForType = (type: string): CredentialPublic[] => {
    if (!userCredentials?.data) return []
    return userCredentials.data.filter((c) => c.type === type && !c.is_placeholder)
  }

  // Handle selection change for shareable credentials (use shared vs own)
  const handleShareableSelectionChange = (
    typeReq: TypeRequirement,
    selectedId: string | null
  ) => {
    const newSelections = { ...credentialSelections }

    // Apply selection to all shareable credentials of this type
    for (const credName of typeReq.shareableCredNames) {
      newSelections[credName] = {
        sourceCredentialName: credName,
        sourceCredentialType: typeReq.type,
        allowSharing: true,
        selectedCredentialId: selectedId,
      }
    }

    onChange(newSelections)
  }

  // Handle selection change for non-shareable credentials
  const handleNonShareableSelectionChange = (
    typeReq: TypeRequirement,
    index: number,
    selectedId: string | null
  ) => {
    const credName = typeReq.nonShareableCredNames[index]
    if (!credName) return

    onChange({
      ...credentialSelections,
      [credName]: {
        sourceCredentialName: credName,
        sourceCredentialType: typeReq.type,
        allowSharing: false,
        selectedCredentialId: selectedId,
      },
    })
  }

  // Get current selection for shareable credentials of a type
  const getShareableSelection = (typeReq: TypeRequirement): string | null => {
    // Return selection from first shareable credential (they should all be the same)
    const firstCredName = typeReq.shareableCredNames[0]
    if (!firstCredName) return null
    return credentialSelections[firstCredName]?.selectedCredentialId ?? null
  }

  // Get current selection for a specific non-shareable credential
  const getNonShareableSelection = (typeReq: TypeRequirement, index: number): string | null => {
    const credName = typeReq.nonShareableCredNames[index]
    if (!credName) return null
    return credentialSelections[credName]?.selectedCredentialId ?? null
  }

  // Open create dialog
  const handleOpenCreateDialog = (type: string, nonShareableIndex: number | null) => {
    setCreatingForType(type)
    setCreatingForIndex(nonShareableIndex)
    setNewCredentialName("")
    setCreateDialogOpen(true)
  }

  // Create credential mutation
  const createMutation = useMutation({
    mutationFn: (data: CredentialCreate) =>
      CredentialsService.createCredential({ requestBody: data }),
    onSuccess: (newCred) => {
      showSuccessToast("Credential created successfully")
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      setCreateDialogOpen(false)

      // Auto-select the newly created credential
      if (creatingForType) {
        const typeReq = typeRequirements.find(t => t.type === creatingForType)
        if (typeReq) {
          if (creatingForIndex !== null) {
            // For non-shareable
            handleNonShareableSelectionChange(typeReq, creatingForIndex, newCred.id)
          } else {
            // For shareable override
            handleShareableSelectionChange(typeReq, newCred.id)
          }
        }
      }

      setCreatingForType(null)
      setCreatingForIndex(null)
      setNewCredentialName("")
    },
    onError: handleError.bind(showErrorToast),
  })

  const handleCreateCredential = () => {
    if (!creatingForType || !newCredentialName.trim()) return
    createMutation.mutate({
      name: newCredentialName.trim(),
      type: creatingForType as CredentialCreate["type"],
    })
  }

  // Check if all non-shareable credentials have selections
  const allNonShareableHaveSelections = useMemo(() => {
    for (const typeReq of typeRequirements) {
      for (let i = 0; i < typeReq.nonShareableCount; i++) {
        const credName = typeReq.nonShareableCredNames[i]
        const selection = credentialSelections[credName]
        if (!selection?.selectedCredentialId || selection.selectedCredentialId === "__create_new__") {
          return false
        }
      }
    }
    return true
  }, [typeRequirements, credentialSelections])

  const hasNonShareable = typeRequirements.some(t => t.nonShareableCount > 0)

  if (isLoadingCredentials) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        <span className="ml-2 text-muted-foreground">Loading credentials...</span>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <p className="text-sm text-muted-foreground">
        This agent requires the following credential types. For each type, you can use
        credentials shared by the owner (if available) or select your own.
      </p>

      <p className="text-sm text-muted-foreground">
        <span className="font-medium">Note:</span> If you create new credentials here, you'll need to
        configure them with your actual account details later in the Credentials settings.
      </p>

      {/* Credentials grouped by type */}
      {typeRequirements.map((typeReq) => {
        const typeLabel = CREDENTIAL_TYPE_LABELS[typeReq.type] || typeReq.type
        const userCreds = getUserCredentialsForType(typeReq.type)

        return (
          <div key={typeReq.type} className="space-y-3">
            <h4 className="font-medium flex items-center gap-2">
              <Badge variant="outline">{typeLabel}</Badge>
              {typeReq.hasShareable && (
                <Badge className="bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200 text-xs">
                  Shared available
                </Badge>
              )}
              {typeReq.nonShareableCount > 0 && !typeReq.hasShareable && (
                <Badge className="bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200 text-xs">
                  Your credential required
                </Badge>
              )}
            </h4>

            <div className="space-y-3 pl-4 border-l-2 border-muted">
              {/* Shareable credentials option */}
              {typeReq.hasShareable && (
                <div className="p-3 bg-green-50 dark:bg-green-950/20 rounded-lg border border-green-200 dark:border-green-800">
                  <div className="flex items-center gap-2 mb-3">
                    <CheckCircle className="h-4 w-4 text-green-600" />
                    <span className="text-sm text-green-800 dark:text-green-200">
                      Owner is sharing {typeReq.shareableCredNames.length > 1
                        ? `${typeReq.shareableCredNames.length} ${typeLabel} credentials`
                        : `a ${typeLabel} credential`}
                    </span>
                  </div>

                  <div>
                    <Label className="text-xs text-muted-foreground mb-1 block">
                      Use shared or select your own:
                    </Label>
                    <Select
                      value={getShareableSelection(typeReq) || "shared"}
                      onValueChange={(value) => {
                        if (value === "__create_new__") {
                          handleOpenCreateDialog(typeReq.type, null)
                        } else {
                          handleShareableSelectionChange(
                            typeReq,
                            value === "shared" ? null : value
                          )
                        }
                      }}
                    >
                      <SelectTrigger className="w-full">
                        <SelectValue placeholder="Select credential" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="shared">
                          <span className="flex items-center gap-2">
                            <CheckCircle className="h-3 w-3 text-green-600" />
                            Use shared from owner
                          </span>
                        </SelectItem>
                        {userCreds.length > 0 && (
                          <>
                            <div className="px-2 py-1 text-xs text-muted-foreground border-t mt-1 pt-1">
                              Your credentials:
                            </div>
                            {userCreds.map((userCred) => (
                              <SelectItem key={userCred.id} value={userCred.id}>
                                {userCred.name}
                              </SelectItem>
                            ))}
                          </>
                        )}
                        <div className="border-t mt-1 pt-1">
                          <SelectItem value="__create_new__">
                            <span className="flex items-center gap-2 text-primary">
                              <Plus className="h-3 w-3" />
                              Create new credential...
                            </span>
                          </SelectItem>
                        </div>
                      </SelectContent>
                    </Select>
                  </div>
                </div>
              )}

              {/* Non-shareable credentials - user must provide their own */}
              {typeReq.nonShareableCount > 0 && (
                <>
                  {Array.from({ length: typeReq.nonShareableCount }).map((_, index) => {
                    const selection = getNonShareableSelection(typeReq, index)
                    const hasSelection = selection && selection !== "__create_new__"

                    return (
                      <div
                        key={`${typeReq.type}-nonshareable-${index}`}
                        className={`p-3 rounded-lg border ${
                          hasSelection
                            ? "bg-green-50 dark:bg-green-950/20 border-green-200 dark:border-green-800"
                            : "bg-yellow-50 dark:bg-yellow-950/20 border-yellow-200 dark:border-yellow-800"
                        }`}
                      >
                        <div className="flex items-center gap-2 mb-3">
                          {hasSelection ? (
                            <CheckCircle className="h-4 w-4 text-green-600" />
                          ) : (
                            <AlertCircle className="h-4 w-4 text-yellow-600" />
                          )}
                          <span className={`text-sm ${hasSelection ? "text-green-800 dark:text-green-200" : "text-yellow-800 dark:text-yellow-200"}`}>
                            {typeReq.nonShareableCount > 1
                              ? `${typeLabel} credential ${index + 1} of ${typeReq.nonShareableCount}`
                              : `${typeLabel} credential`}
                            {!hasSelection && " - requires your own"}
                          </span>
                        </div>

                        <div>
                          <Label className="text-xs text-muted-foreground mb-1 block">
                            Select your credential:
                          </Label>
                          <Select
                            value={selection || ""}
                            onValueChange={(value) => {
                              if (value === "__create_new__") {
                                handleOpenCreateDialog(typeReq.type, index)
                              } else {
                                handleNonShareableSelectionChange(typeReq, index, value)
                              }
                            }}
                          >
                            <SelectTrigger className="w-full">
                              <SelectValue placeholder="Select a credential..." />
                            </SelectTrigger>
                            <SelectContent>
                              {userCreds.length > 0 ? (
                                <>
                                  {userCreds.map((userCred) => (
                                    <SelectItem key={userCred.id} value={userCred.id}>
                                      {userCred.name}
                                    </SelectItem>
                                  ))}
                                </>
                              ) : (
                                <div className="px-2 py-2 text-xs text-muted-foreground">
                                  No {typeLabel} credentials found
                                </div>
                              )}
                              <div className="border-t mt-1 pt-1">
                                <SelectItem value="__create_new__">
                                  <span className="flex items-center gap-2 text-primary">
                                    <Plus className="h-3 w-3" />
                                    Create new credential...
                                  </span>
                                </SelectItem>
                              </div>
                            </SelectContent>
                          </Select>
                        </div>
                      </div>
                    )
                  })}
                </>
              )}
            </div>
          </div>
        )
      })}

      {/* Create Credential Dialog */}
      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              if (newCredentialName.trim() && !createMutation.isPending) {
                handleCreateCredential()
              }
            }}
          >
            <DialogHeader>
              <DialogTitle>Create New Credential</DialogTitle>
              <DialogDescription>
                Create a new {creatingForType ? CREDENTIAL_TYPE_LABELS[creatingForType] || creatingForType : ""} credential.
                You can configure the details after accepting the share.
              </DialogDescription>
            </DialogHeader>
            <div className="py-4">
              <Label htmlFor="credential-name">Credential Name</Label>
              <Input
                id="credential-name"
                value={newCredentialName}
                onChange={(e) => setNewCredentialName(e.target.value)}
                placeholder="My Credential"
                className="mt-2"
                autoFocus
              />
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setCreateDialogOpen(false)}
                disabled={createMutation.isPending}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={!newCredentialName.trim() || createMutation.isPending}
              >
                {createMutation.isPending ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Creating...
                  </>
                ) : (
                  "Create"
                )}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Actions */}
      <div className="flex justify-between pt-4 border-t">
        <Button variant="outline" onClick={onBack}>
          Back
        </Button>
        <div className="flex gap-2">
          {!allNonShareableHaveSelections && hasNonShareable && (
            <Button variant="ghost" onClick={onNext}>
              Skip for now
            </Button>
          )}
          <Button
            onClick={onNext}
            disabled={!allNonShareableHaveSelections && hasNonShareable}
          >
            Continue
          </Button>
        </div>
      </div>
    </div>
  )
}
