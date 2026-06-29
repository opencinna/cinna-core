import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, Copy, Key, Loader2 } from "lucide-react"

import type { SSHKeyPublic } from "@/client"
import { SshKeysService } from "@/client"
import { getErrorMessage } from "@/utils"
import useCustomToast from "@/hooks/useCustomToast"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

// Sentinel option values that don't correspond to a real key id.
const NONE_VALUE = "__none__"
const GENERATE_VALUE = "__generate__"

interface DeployKeySelectProps {
  /** Currently selected ssh key id, or null for "None (public repo)". */
  value: string | null
  /** Called with the chosen key id, or null for a public repo. */
  onChange: (sshKeyId: string | null) => void
  disabled?: boolean
}

/**
 * Deploy-key picker for the git versioning connect flow.
 *
 * Reuses the ssh_keys feature: list the user's keys, pick one, choose
 * "None (public repo)", or quick-generate a new key. After generating, the
 * PUBLIC key is shown in a copyable block with deploy-key guidance and the new
 * key is auto-selected. Private-key material is never displayed.
 */
export function DeployKeySelect({
  value,
  onChange,
  disabled,
}: DeployKeySelectProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [generating, setGenerating] = useState(false)
  const [keyName, setKeyName] = useState("")
  const [generatedKey, setGeneratedKey] = useState<SSHKeyPublic | null>(null)
  const [copied, setCopied] = useState(false)

  const { data: keysData, isLoading } = useQuery({
    queryKey: ["sshKeys"],
    queryFn: () => SshKeysService.readSshKeys(),
  })
  const keys = keysData?.data ?? []

  const generateMutation = useMutation({
    mutationFn: (name: string) =>
      SshKeysService.generateSshKey({ requestBody: { name } }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["sshKeys"] })
      setGeneratedKey(data)
      // Collapse the generate sub-flow so the Select trigger shows the newly
      // selected key's name (the public-key block stays visible via generatedKey).
      setGenerating(false)
      onChange(data.id) // auto-select the freshly generated key
      showSuccessToast("SSH key generated")
    },
    onError: (error) => {
      showErrorToast(getErrorMessage(error, "Failed to generate SSH key"))
    },
  })

  const handleSelectChange = (next: string) => {
    if (next === GENERATE_VALUE) {
      setGenerating(true)
      setGeneratedKey(null)
      return
    }
    setGenerating(false)
    setGeneratedKey(null)
    onChange(next === NONE_VALUE ? null : next)
  }

  const handleGenerate = () => {
    const name = keyName.trim()
    if (!name) return
    generateMutation.mutate(name)
  }

  const copyPublicKey = () => {
    if (!generatedKey) return
    navigator.clipboard.writeText(generatedKey.public_key)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  // While the generate sub-flow is open, keep the select pinned to the
  // generate option; otherwise reflect the chosen key (or "None").
  const selectValue = generating ? GENERATE_VALUE : value ?? NONE_VALUE

  return (
    <div className="space-y-2">
      <Label className="flex items-center gap-2">
        <Key className="h-4 w-4" />
        Deploy key
      </Label>
      <Select
        value={selectValue}
        onValueChange={handleSelectChange}
        disabled={disabled}
      >
        <SelectTrigger>
          <SelectValue placeholder="Select a deploy key" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NONE_VALUE}>None (public repo)</SelectItem>
          {keys.map((key) => (
            <SelectItem key={key.id} value={key.id}>
              {key.name}
            </SelectItem>
          ))}
          <SelectItem value={GENERATE_VALUE}>Generate a new key…</SelectItem>
        </SelectContent>
      </Select>

      {isLoading && (
        <p className="text-xs text-muted-foreground">Loading keys…</p>
      )}

      {generating && !generatedKey && (
        <div className="space-y-2 rounded-md border p-3">
          <Label htmlFor="deploy-key-name">Key name</Label>
          <div className="flex gap-2">
            <Input
              id="deploy-key-name"
              placeholder="e.g., Cinna Deploy Key"
              value={keyName}
              onChange={(e) => setKeyName(e.target.value)}
              disabled={generateMutation.isPending}
            />
            <Button
              type="button"
              onClick={handleGenerate}
              disabled={!keyName.trim() || generateMutation.isPending}
            >
              {generateMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                "Generate"
              )}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            An RSA key pair is generated. The private key is encrypted and kept
            on the platform — you will only ever see the public key.
          </p>
        </div>
      )}

      {generatedKey && (
        <div className="space-y-2 rounded-md border p-3">
          <div className="flex items-center justify-between">
            <Label>Public key</Label>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={copyPublicKey}
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
            className="font-mono text-xs h-24"
          />
          <p className="text-xs text-muted-foreground">
            Add this as a <strong>Deploy key</strong> in your GitHub/GitLab repo
            settings and check <strong>“Allow write access”</strong> so the
            platform can push.
          </p>
        </div>
      )}
    </div>
  )
}
