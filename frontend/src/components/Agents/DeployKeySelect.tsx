import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Key } from "lucide-react"

import type { SSHKeyPublic } from "@/client"
import { SshKeysService } from "@/client"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { GenerateKeyModal } from "@/components/UserSettings/GenerateKeyModal"
import { ImportKeyModal } from "@/components/UserSettings/ImportKeyModal"

// Sentinel option values that don't correspond to a real key id.
const NONE_VALUE = "__none__"
const GENERATE_VALUE = "__generate__"
const IMPORT_VALUE = "__import__"

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
 * "None (public repo)", or add a new key. Generating / importing a key reuses
 * the exact same modal dialogs as the Settings → SSH Keys management screen
 * (`GenerateKeyModal` / `ImportKeyModal`); the freshly created key is
 * auto-selected. Private-key material is never displayed.
 */
export function DeployKeySelect({
  value,
  onChange,
  disabled,
}: DeployKeySelectProps) {
  const [generateOpen, setGenerateOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)

  const { data: keysData, isLoading } = useQuery({
    queryKey: ["sshKeys"],
    queryFn: () => SshKeysService.readSshKeys(),
  })
  const keys = keysData?.data ?? []

  const handleSelectChange = (next: string) => {
    if (next === GENERATE_VALUE) {
      setGenerateOpen(true)
      return
    }
    if (next === IMPORT_VALUE) {
      setImportOpen(true)
      return
    }
    onChange(next === NONE_VALUE ? null : next)
  }

  const handleNewKey = (key: SSHKeyPublic) => {
    onChange(key.id) // auto-select the freshly created key
  }

  return (
    <div className="flex items-center justify-between gap-4">
      <div className="space-y-0.5">
        <Label className="flex items-center gap-2">
          <Key className="h-4 w-4" />
          Deploy key
        </Label>
        <p className="text-xs text-muted-foreground">
          Add the key's public half as a <strong>Deploy key</strong> in your
          GitHub/GitLab repo settings and check{" "}
          <strong>“Allow write access”</strong> so the platform can push.
        </p>
        {isLoading && (
          <p className="text-xs text-muted-foreground">Loading keys…</p>
        )}
      </div>
      <Select
        value={value ?? NONE_VALUE}
        onValueChange={handleSelectChange}
        disabled={disabled}
      >
        <SelectTrigger className="w-[200px] shrink-0">
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
          <SelectItem value={IMPORT_VALUE}>Import an existing key…</SelectItem>
        </SelectContent>
      </Select>

      <GenerateKeyModal
        open={generateOpen}
        onClose={() => setGenerateOpen(false)}
        onGenerated={handleNewKey}
      />
      <ImportKeyModal
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onImported={handleNewKey}
      />
    </div>
  )
}
