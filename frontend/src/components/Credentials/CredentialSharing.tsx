import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Box, Share2, Trash2, Users, AlertTriangle } from "lucide-react"
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
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Alert,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import useCustomToast from "@/hooks/useCustomToast"
import useRole from "@/hooks/useRole"
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
  const { isAgentUser } = useRole()
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

  // Bundles whose publisher install has this credential linked. Shown
  // on the sharing card so the owner can see at a glance where their
  // credential is in use across the bundles they publish.
  const { data: bundleUsages } = useQuery({
    queryKey: ["credential-bundle-usages", credential.id],
    queryFn: () =>
      CredentialsService.listCredentialBundleUsages({ id: credential.id }),
  })

  // Deletion-impact also covers the disable-sharing blast radius: when this
  // credential is publisher-provided (PBP) in published bundles, disabling
  // sharing revokes the publisher shares and breaks those installs — the same
  // class of impact as a delete. Surfaced in the disable-sharing dialog.
  const { data: deletionImpact } = useQuery({
    queryKey: ["credential-deletion-impact", credential.id],
    queryFn: () =>
      CredentialsService.getCredentialDeletionImpact({ id: credential.id }),
    // Only fetch when the disable-sharing dialog is open. Shares the cache
    // key with the delete dialog so either entry point warms the same data.
    enabled: isDisableDialogOpen,
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

  if (isAgentUser) {
    return null
  }

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
        <div className="flex items-start justify-between">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2">
              <Share2 className="h-5 w-5" />
              Sharing
            </CardTitle>
            <CardDescription>
              {allowSharing
                ? "Share this credential with other users to allow them to use it in their agents."
                : "Enable to share this credential with other users."}
            </CardDescription>
          </div>
          <label className="flex cursor-pointer select-none items-center ml-4 mt-1">
            <div className="relative">
              <input
                type="checkbox"
                checked={allowSharing}
                onChange={(e) => handleToggleSharing(e.target.checked)}
                disabled={toggleSharingMutation.isPending}
                className="sr-only"
              />
              <div
                className={`block h-6 w-11 rounded-full transition-colors ${
                  allowSharing ? "bg-emerald-500" : "bg-gray-300 dark:bg-gray-600"
                }`}
              />
              <div
                className={`dot absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                  allowSharing ? "translate-x-5" : ""
                }`}
              />
            </div>
          </label>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
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

        {(() => {
          const sharedUsages = (bundleUsages?.data ?? []).filter(
            (u) => u.provided_by === "publisher",
          )
          if (sharedUsages.length === 0) return null
          return (
            <div className="space-y-2 pt-2">
              <h4 className="text-sm font-medium">Used in Bundles</h4>
              <p className="text-xs text-muted-foreground">
                Bundles that ship this credential as a fully shared
                publisher credential.
              </p>
              <ul className="space-y-1.5">
                {sharedUsages.map((usage) => (
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
          )
        })()}

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
            {(() => {
              const pbpUsages = (deletionImpact?.bundle_pbp_usages ?? [])
              const activeInstalls = deletionImpact?.active_install_count ?? 0
              if (pbpUsages.length === 0) return null
              return (
                <div className="space-y-2 pt-1">
                  <Alert variant="destructive">
                    <Box className="h-4 w-4" />
                    <AlertTitle>This breaks published bundles</AlertTitle>
                    <AlertDescription>
                      This credential is provided by the publisher in{" "}
                      {pbpUsages.length} published bundle
                      {pbpUsages.length !== 1 ? "s" : ""}
                      {activeInstalls > 0 && (
                        <>
                          {" "}with {activeInstalls} active install
                          {activeInstalls !== 1 ? "s" : ""}
                        </>
                      )}
                      . Disabling sharing will leave those installs without their
                      publisher-provided credentials.
                    </AlertDescription>
                  </Alert>
                  <ul className="space-y-1.5">
                    {pbpUsages.map((usage) => (
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
                              params={{ agentId: usage.publisher_install_id }}
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
              )
            })()}
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
