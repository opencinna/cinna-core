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
import { Copy, Check } from "lucide-react"

interface GenerateKeyModalProps {
  open: boolean
  onClose: () => void
  /** Called once the key has been generated (e.g. to auto-select it). */
  onGenerated?: (key: SSHKeyPublic) => void
}

export function GenerateKeyModal({
  open,
  onClose,
  onGenerated,
}: GenerateKeyModalProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [keyName, setKeyName] = useState("")
  const [generatedKey, setGeneratedKey] = useState<SSHKeyPublic | null>(null)
  const [copied, setCopied] = useState(false)

  // Reset form when modal opens/closes
  useEffect(() => {
    if (!open) {
      setKeyName("")
      setGeneratedKey(null)
      setCopied(false)
    }
  }, [open])

  const generateMutation = useMutation({
    mutationFn: (name: string) =>
      SshKeysService.generateSshKey({
        requestBody: { name }
      }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["sshKeys"] })
      setGeneratedKey(data)
      onGenerated?.(data)
      showSuccessToast("SSH key generated successfully")
    },
    onError: (error: any) => {
      showErrorToast(error.body?.detail || "Failed to generate SSH key")
    },
  })

  const handleGenerate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!keyName.trim()) return

    generateMutation.mutate(keyName.trim())
  }

  const copyToClipboard = () => {
    if (generatedKey) {
      navigator.clipboard.writeText(generatedKey.public_key)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  const handleClose = () => {
    onClose()
  }

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-[600px]">
        {!generatedKey ? (
          <form onSubmit={handleGenerate}>
            <DialogHeader>
              <DialogTitle>Generate SSH Key</DialogTitle>
              <DialogDescription>
                Generate a new RSA 4096-bit SSH key pair. The private key will be encrypted and stored securely.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="key-name">Key Name</Label>
                <Input
                  id="key-name"
                  placeholder="e.g., GitHub Deploy Key - Docs"
                  value={keyName}
                  onChange={(e) => setKeyName(e.target.value)}
                  autoFocus
                  required
                />
                <p className="text-xs text-muted-foreground">
                  Choose a descriptive name to identify this key
                </p>
              </div>
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={handleClose}>
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={!keyName.trim() || generateMutation.isPending}
              >
                {generateMutation.isPending ? "Generating..." : "Generate Key"}
              </Button>
            </DialogFooter>
          </form>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>SSH Key Generated</DialogTitle>
              <DialogDescription>
                Your SSH key has been generated and stored securely. Copy the public key below and add it to your Git provider.
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <div className="flex items-center justify-between">
                  <Label>Public Key</Label>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={copyToClipboard}
                  >
                    {copied ? (
                      <>
                        <Check className="h-4 w-4 mr-2" />
                        Copied!
                      </>
                    ) : (
                      <>
                        <Copy className="h-4 w-4 mr-2" />
                        Copy
                      </>
                    )}
                  </Button>
                </div>
                <Textarea
                  value={generatedKey.public_key}
                  readOnly
                  className="font-mono text-xs h-32"
                />
                <p className="text-xs text-muted-foreground">
                  Add this public key to your Git provider's deploy keys or SSH keys settings.
                  The private key is encrypted and stored securely - you won't see it again.
                </p>
              </div>
              <div className="grid gap-2">
                <Label>Fingerprint</Label>
                <Input
                  value={generatedKey.fingerprint}
                  readOnly
                  className="font-mono text-xs"
                />
              </div>
            </div>
            <DialogFooter>
              <Button onClick={handleClose}>
                Done
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
