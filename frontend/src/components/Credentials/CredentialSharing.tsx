import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Share2, Trash2, Users, AlertTriangle } from "lucide-react"
import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { CredentialsService } from "@/client"
import type { CredentialPublic, CredentialSharePublic } from "@/client"
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
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Alert,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

interface CredentialSharingProps {
  credential: CredentialPublic
}

const shareSchema = z.object({
  shared_with_email: z.string().email({ message: "Please enter a valid email address" }),
})

type ShareFormData = z.infer<typeof shareSchema>

function SharesList({
  credentialId,
  shares,
  isLoading,
}: {
  credentialId: string
  shares: CredentialSharePublic[]
  isLoading: boolean
}) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const revokeMutation = useMutation({
    mutationFn: (shareId: string) =>
      CredentialsService.revokeCredentialShare({
        credentialId,
        shareId,
      }),
    onSuccess: () => {
      showSuccessToast("Share revoked successfully")
      queryClient.invalidateQueries({ queryKey: ["credential-shares", credentialId] })
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({ queryKey: ["credential", credentialId] })
    },
    onError: handleError.bind(showErrorToast),
  })

  if (isLoading) {
    return <div className="text-sm text-muted-foreground">Loading shares...</div>
  }

  if (shares.length === 0) {
    return (
      <div className="text-sm text-muted-foreground py-4 text-center">
        This credential is not shared with anyone yet.
      </div>
    )
  }

  return (
    <div className="space-y-2">
      {shares.map((share) => (
        <div
          key={share.id}
          className="flex items-center justify-between p-3 rounded-lg border bg-muted/30"
        >
          <div className="flex items-center gap-3">
            <div className="rounded-full bg-primary/10 p-2">
              <Users className="h-4 w-4 text-primary" />
            </div>
            <div>
              <div className="text-sm font-medium">{share.shared_with_email}</div>
              <div className="text-xs text-muted-foreground">
                Shared {new Date(share.shared_at).toLocaleDateString()}
              </div>
            </div>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => revokeMutation.mutate(share.id)}
            disabled={revokeMutation.isPending}
          >
            <Trash2 className="h-4 w-4 text-destructive" />
          </Button>
        </div>
      ))}
    </div>
  )
}

export function CredentialSharing({ credential }: CredentialSharingProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [isShareDialogOpen, setIsShareDialogOpen] = useState(false)
  const [isDisableDialogOpen, setIsDisableDialogOpen] = useState(false)
  const [allowSharing, setAllowSharing] = useState(credential.allow_sharing ?? false)

  // Sync local state when prop changes (e.g., after query refetch)
  useEffect(() => {
    setAllowSharing(credential.allow_sharing ?? false)
  }, [credential.allow_sharing])

  const { data: sharesData, isLoading: sharesLoading } = useQuery({
    queryKey: ["credential-shares", credential.id],
    queryFn: () => CredentialsService.getCredentialShares({ credentialId: credential.id }),
    enabled: allowSharing,
  })

  const shareForm = useForm<ShareFormData>({
    resolver: zodResolver(shareSchema),
    defaultValues: {
      shared_with_email: "",
    },
  })

  const shareMutation = useMutation({
    mutationFn: (data: ShareFormData) =>
      CredentialsService.shareCredential({
        credentialId: credential.id,
        requestBody: data,
      }),
    onSuccess: () => {
      showSuccessToast("Credential shared successfully")
      shareForm.reset()
      setIsShareDialogOpen(false)
      queryClient.invalidateQueries({ queryKey: ["credential-shares", credential.id] })
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({ queryKey: ["credential", credential.id] })
    },
    onError: handleError.bind(showErrorToast),
  })

  const toggleSharingMutation = useMutation({
    mutationFn: (newAllowSharing: boolean) =>
      CredentialsService.updateCredentialSharing({
        credentialId: credential.id,
        requestBody: { allow_sharing: newAllowSharing },
      }),
    onSuccess: (_, newAllowSharing) => {
      setAllowSharing(newAllowSharing)
      showSuccessToast(
        newAllowSharing
          ? "Sharing enabled for this credential"
          : "Sharing disabled. All shares have been revoked."
      )
      setIsDisableDialogOpen(false)
      queryClient.invalidateQueries({ queryKey: ["credentials"] })
      queryClient.invalidateQueries({ queryKey: ["credential", credential.id] })
      queryClient.invalidateQueries({ queryKey: ["credential-shares", credential.id] })
    },
    onError: handleError.bind(showErrorToast),
  })

  const shares = sharesData?.data ?? []
  const shareCount = credential.share_count ?? 0

  const handleToggleSharing = (checked: boolean) => {
    if (!checked && shareCount > 0) {
      // Show confirmation dialog before disabling
      setIsDisableDialogOpen(true)
    } else {
      toggleSharingMutation.mutate(checked)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Share2 className="h-5 w-5" />
          Sharing
        </CardTitle>
        <CardDescription>
          Share this credential with other users to allow them to use it in their agents.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Sharing Toggle */}
        <div className="flex items-center justify-between p-4 rounded-lg border">
          <div className="space-y-0.5">
            <Label htmlFor="allow-sharing" className="text-base">
              Allow Sharing
            </Label>
            <div className="text-sm text-muted-foreground">
              Enable to share this credential with other users
            </div>
          </div>
          <Switch
            id="allow-sharing"
            checked={allowSharing}
            onCheckedChange={handleToggleSharing}
            disabled={toggleSharingMutation.isPending}
          />
        </div>

        {/* Shares List (only when sharing is enabled) */}
        {allowSharing && (
          <>
            <div className="flex items-center justify-between">
              <h4 className="text-sm font-medium">
                Shared with {shareCount} user{shareCount !== 1 ? "s" : ""}
              </h4>
              <Dialog open={isShareDialogOpen} onOpenChange={setIsShareDialogOpen}>
                <DialogTrigger asChild>
                  <Button variant="outline" size="sm">
                    <Share2 className="mr-2 h-4 w-4" />
                    Share
                  </Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>Share Credential</DialogTitle>
                    <DialogDescription>
                      Enter the email address of the user you want to share this credential with.
                      They will be able to use this credential in their agents but won't see the
                      actual credentials values.
                    </DialogDescription>
                  </DialogHeader>
                  <Form {...shareForm}>
                    <form
                      onSubmit={shareForm.handleSubmit((data) => shareMutation.mutate(data))}
                      className="space-y-4"
                    >
                      <FormField
                        control={shareForm.control}
                        name="shared_with_email"
                        render={({ field }) => (
                          <FormItem>
                            <FormLabel>Email Address</FormLabel>
                            <FormControl>
                              <Input
                                placeholder="user@example.com"
                                type="email"
                                {...field}
                              />
                            </FormControl>
                            <FormMessage />
                          </FormItem>
                        )}
                      />
                      <DialogFooter>
                        <Button
                          type="button"
                          variant="outline"
                          onClick={() => setIsShareDialogOpen(false)}
                        >
                          Cancel
                        </Button>
                        <LoadingButton type="submit" loading={shareMutation.isPending}>
                          Share
                        </LoadingButton>
                      </DialogFooter>
                    </form>
                  </Form>
                </DialogContent>
              </Dialog>
            </div>

            <SharesList
              credentialId={credential.id}
              shares={shares}
              isLoading={sharesLoading}
            />
          </>
        )}

        {/* Disable Sharing Confirmation Dialog */}
        <Dialog open={isDisableDialogOpen} onOpenChange={setIsDisableDialogOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Disable Sharing?</DialogTitle>
              <DialogDescription>
                This will revoke access for all users this credential is currently shared with.
              </DialogDescription>
            </DialogHeader>
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertTitle>Warning</AlertTitle>
              <AlertDescription>
                {shareCount} user{shareCount !== 1 ? "s" : ""} will lose access to this credential
                immediately. This action cannot be undone.
              </AlertDescription>
            </Alert>
            <DialogFooter>
              <Button variant="outline" onClick={() => setIsDisableDialogOpen(false)}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={() => toggleSharingMutation.mutate(false)}
                disabled={toggleSharingMutation.isPending}
              >
                Disable Sharing
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  )
}
