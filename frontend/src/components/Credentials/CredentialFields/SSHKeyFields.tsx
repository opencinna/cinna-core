import { Control, UseFormWatch } from "react-hook-form"
import { ShieldAlert } from "lucide-react"
import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Tabs,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs"
import {
  Alert,
  AlertDescription,
} from "@/components/ui/alert"

interface SSHKeyFieldsProps {
  control: Control<any>
  watch: UseFormWatch<any>
}

/**
 * SSH key credential fields — used at create time inside AddCredential's
 * dedicated ssh_key dialog. Two modes:
 *   - generate: server generates the pair (key_type: rsa | ed25519)
 *   - import: user pastes an existing public + private key (passphrase
 *     unsupported in MVP; server rejects encrypted keys with a 422).
 *
 * Host aliases are collected as a comma-separated string. Empty input means
 * "apply to all hosts"; the server defaults this to ["*"].
 */
export function SSHKeyFields({ control, watch }: SSHKeyFieldsProps) {
  const mode = watch("credential_data.mode") || "generate"

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
      {/* Left column — basic credential metadata */}
      <div className="space-y-4">
        <FormField
          control={control}
          name="name"
          render={({ field }) => (
            <FormItem>
              <FormLabel>
                Name <span className="text-destructive">*</span>
              </FormLabel>
              <FormControl>
                <Input placeholder="GitHub deploy - Monorepo" type="text" {...field} />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />

        <FormField
          control={control}
          name="notes"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Notes</FormLabel>
              <FormControl>
                <Textarea
                  placeholder="Additional notes..."
                  className="min-h-[120px]"
                  {...field}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />

        <Alert>
          <ShieldAlert className="h-4 w-4" />
          <AlertDescription className="text-xs">
            Private keys are encrypted and never displayed after creation.
            The agent container is the only place that sees the decrypted key.
          </AlertDescription>
        </Alert>
      </div>

      {/* Right column — mode toggle + type-specific fields */}
      <div className="space-y-4">
        <FormField
          control={control}
          name="credential_data.mode"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Source</FormLabel>
              <FormControl>
                <Tabs
                  value={field.value || "generate"}
                  onValueChange={field.onChange}
                  className="w-full"
                >
                  <TabsList className="grid w-full grid-cols-2">
                    <TabsTrigger value="generate">Generate new key</TabsTrigger>
                    <TabsTrigger value="import">Import existing key</TabsTrigger>
                  </TabsList>
                </Tabs>
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />

        {mode === "generate" && (
          <FormField
            control={control}
            name="credential_data.key_type"
            render={({ field }) => (
              <FormItem>
                <FormLabel>
                  Key Type <span className="text-destructive">*</span>
                </FormLabel>
                <Select
                  onValueChange={field.onChange}
                  value={(field.value as string) || "ed25519"}
                >
                  <FormControl>
                    <SelectTrigger>
                      <SelectValue placeholder="Select key type" />
                    </SelectTrigger>
                  </FormControl>
                  <SelectContent>
                    <SelectItem value="ed25519">Ed25519 (recommended)</SelectItem>
                    <SelectItem value="rsa">RSA 4096</SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground mt-1">
                  Ed25519 is faster and shorter. RSA 4096 offers broader
                  compatibility with legacy servers.
                </p>
                <FormMessage />
              </FormItem>
            )}
          />
        )}

        {mode === "import" && (
          <>
            <FormField
              control={control}
              name="credential_data.public_key"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>
                    Public Key <span className="text-destructive">*</span>
                  </FormLabel>
                  <FormControl>
                    <Textarea
                      placeholder="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5..."
                      className="font-mono text-xs min-h-[80px]"
                      {...field}
                    />
                  </FormControl>
                  <p className="text-xs text-muted-foreground mt-1">
                    Must start with <code>ssh-rsa</code>, <code>ssh-ed25519</code>,{" "}
                    <code>ssh-dss</code>, or <code>ecdsa-sha2-*</code>.
                  </p>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={control}
              name="credential_data.private_key"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>
                    Private Key <span className="text-destructive">*</span>
                  </FormLabel>
                  <FormControl>
                    <Textarea
                      placeholder={"-----BEGIN OPENSSH PRIVATE KEY-----\n..."}
                      className="font-mono text-xs min-h-[160px]"
                      {...field}
                    />
                  </FormControl>
                  <p className="text-xs text-muted-foreground mt-1">
                    Must contain PEM markers. Passphrase-encrypted keys are not
                    supported — please export without a passphrase or generate
                    a new key instead.
                  </p>
                  <FormMessage />
                </FormItem>
              )}
            />
          </>
        )}

        <FormField
          control={control}
          name="credential_data.host_aliases_text"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Host Aliases</FormLabel>
              <FormControl>
                <Input
                  placeholder="github.com, gitlab.com"
                  {...field}
                  value={(field.value as string) ?? ""}
                />
              </FormControl>
              <p className="text-xs text-muted-foreground mt-1">
                Optional — comma-separated list of hosts this key should bind to
                (e.g., <code>github.com, gitlab.com</code>). Leave blank to use
                for all SSH hosts.
              </p>
              <FormMessage />
            </FormItem>
          )}
        />
      </div>
    </div>
  )
}
