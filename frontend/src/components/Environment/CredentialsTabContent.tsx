import { Loader2 } from "lucide-react"
import { Checkbox } from "@/components/ui/checkbox"
import { TabsContent } from "@/components/ui/tabs"
import type { CredentialPublic } from "@/client"

interface CredentialsTabContentProps {
  credentials: CredentialPublic[]
  isLoading: boolean
  isCredentialShared: (credentialId: string) => boolean
  onToggleCredential: (credentialId: string) => void
  isMutating: boolean
  mutatingCredentialId?: string
}

function getCredentialTypeLabel(type: string): string {
  switch (type) {
    case "email_imap":
      return "Email (IMAP)"
    case "email_smtp":
      return "Email (SMTP)"
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
    case "api_token":
      return "API Token"
    default:
      return type
  }
}

export function CredentialsTabContent({
  credentials,
  isLoading,
  isCredentialShared,
  onToggleCredential,
  isMutating,
  mutatingCredentialId,
}: CredentialsTabContentProps) {
  if (isLoading) {
    return (
      <TabsContent value="credentials" className="flex-1 overflow-auto p-4">
        <div className="flex items-center justify-center h-32">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
      </TabsContent>
    )
  }

  if (credentials.length === 0) {
    return (
      <TabsContent value="credentials" className="flex-1 overflow-auto p-4">
        <div className="text-center py-8 text-muted-foreground text-sm">
          No credentials available.
          <br />
          Create credentials in the Credentials page to share with this agent.
        </div>
      </TabsContent>
    )
  }

  return (
    <TabsContent value="credentials" className="flex-1 overflow-auto p-4">
      <div className="text-xs text-muted-foreground mb-3">
        Select credentials to share with this agent. Shared credentials are automatically synced to the agent environment.
      </div>
      <div className="space-y-2">
        {credentials.map((credential) => {
          const isShared = isCredentialShared(credential.id)
          const isCurrentlyMutating = isMutating && mutatingCredentialId === credential.id

          return (
            <div
              key={credential.id}
              className="flex items-center gap-3 p-2 rounded-md hover:bg-muted/50 transition-colors"
            >
              <div className="relative">
                {isCurrentlyMutating ? (
                  <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                ) : (
                  <Checkbox
                    id={`credential-${credential.id}`}
                    checked={isShared}
                    onCheckedChange={() => onToggleCredential(credential.id)}
                    disabled={isMutating}
                  />
                )}
              </div>
              <label
                htmlFor={`credential-${credential.id}`}
                className="flex-1 cursor-pointer select-none"
              >
                <div className="text-sm font-medium">{credential.name}</div>
                <div className="text-xs text-muted-foreground">
                  {getCredentialTypeLabel(credential.type)}
                </div>
              </label>
            </div>
          )
        })}
      </div>
    </TabsContent>
  )
}
