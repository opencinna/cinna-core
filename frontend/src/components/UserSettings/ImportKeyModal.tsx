import { useState, useEffect } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { SshKeysService, type SSHKeyPublic } from "@/client"
import useCustomToast from "@/hooks/useCustomToast"

interface ImportKeyModalProps {
  open: boolean
  onClose: () => void
  /** Called once the key has been imported (e.g. to auto-select it). */
  onImported?: (key: SSHKeyPublic) => void
}

export function ImportKeyModal({
  open,
  onClose,
  onImported,
}: ImportKeyModalProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [keyName, setKeyName] = useState("")
  const [publicKey, setPublicKey] = useState("")
  const [privateKey, setPrivateKey] = useState("")
  const [passphrase, setPassphrase] = useState("")

  // Reset form when modal opens/closes
  useEffect(() => {
    if (!open) {
      setKeyName("")
      setPublicKey("")
      setPrivateKey("")
      setPassphrase("")
    }
  }, [open])

  const importMutation = useMutation({
    mutationFn: (data: { name: string; public_key: string; private_key: string; passphrase?: string }) =>
      SshKeysService.importSshKey({
        requestBody: data
      }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["sshKeys"] })
      onImported?.(data)
      showSuccessToast("SSH key imported successfully")
      onClose()
    },
    onError: (error: any) => {
      showErrorToast(error.body?.detail || "Failed to import SSH key")
    },
  })

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!keyName.trim() || !publicKey.trim() || !privateKey.trim()) return

    importMutation.mutate({
      name: keyName.trim(),
      public_key: publicKey.trim(),
      private_key: privateKey.trim(),
      passphrase: passphrase.trim() || undefined
    })
  }

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-[600px]">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Import SSH Key</DialogTitle>
            <DialogDescription>
              Import an existing SSH key pair. The private key and passphrase will be encrypted and stored securely.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="import-key-name">Key Name *</Label>
              <Input
                id="import-key-name"
                placeholder="e.g., GitHub Deploy Key"
                value={keyName}
                onChange={(e) => setKeyName(e.target.value)}
                autoFocus
                required
              />
            </div>

            <div className="grid gap-2">
              <Label htmlFor="public-key">Public Key *</Label>
              <Textarea
                id="public-key"
                placeholder="ssh-rsa AAAAB3NzaC1yc2EA..."
                value={publicKey}
                onChange={(e) => setPublicKey(e.target.value)}
                className="font-mono text-xs h-24"
                required
              />
              <p className="text-xs text-muted-foreground">
                Should start with ssh-rsa, ssh-ed25519, or similar
              </p>
            </div>

            <div className="grid gap-2">
              <Label htmlFor="private-key">Private Key *</Label>
              <Textarea
                id="private-key"
                placeholder="-----BEGIN RSA PRIVATE KEY-----&#10;..."
                value={privateKey}
                onChange={(e) => setPrivateKey(e.target.value)}
                className="font-mono text-xs h-32"
                required
              />
              <p className="text-xs text-muted-foreground">
                Should contain -----BEGIN ... PRIVATE KEY-----
              </p>
            </div>

            <div className="grid gap-2">
              <Label htmlFor="passphrase">Passphrase (optional)</Label>
              <Input
                id="passphrase"
                type="password"
                placeholder="Leave empty if no passphrase"
                value={passphrase}
                onChange={(e) => setPassphrase(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Only required if your private key is encrypted with a passphrase
              </p>
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                !keyName.trim() ||
                !publicKey.trim() ||
                !privateKey.trim() ||
                importMutation.isPending
              }
            >
              {importMutation.isPending ? "Importing..." : "Import Key"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
