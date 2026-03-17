import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Key, Mail, Database, AtSign, Users } from "lucide-react"

import { CredentialsService } from "@/client"
import type { SharedCredentialPublic } from "@/client"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"

function getCredentialIcon(type: string) {
  switch (type) {
    case "email_imap":
    case "email_smtp":
      return <Mail className="h-5 w-5" />
    case "odoo":
      return <Database className="h-5 w-5" />
    case "gmail_oauth":
    case "gmail_oauth_readonly":
    case "gdrive_oauth":
    case "gdrive_oauth_readonly":
    case "gcalendar_oauth":
    case "gcalendar_oauth_readonly":
      return <AtSign className="h-5 w-5" />
    case "api_token":
      return <Key className="h-5 w-5" />
    default:
      return <Key className="h-5 w-5" />
  }
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

function SharedCredentialCard({ credential }: { credential: SharedCredentialPublic }) {
  return (
    <Link
      to="/credential/$credentialId"
      params={{ credentialId: credential.id }}
      className="block h-full"
    >
      <Card className="relative transition-all hover:shadow-md hover:-translate-y-0.5 cursor-pointer h-full flex flex-col gap-0">
        <CardHeader className="pb-2">
          <div className="flex items-start gap-3">
            <div className="rounded-lg bg-blue-500/10 p-2 text-blue-500">
              {getCredentialIcon(credential.type)}
            </div>
            <div className="flex-1 min-w-0">
              <CardTitle className="text-lg break-words">
                {credential.name}
              </CardTitle>
            </div>
          </div>
          {credential.notes && (
            <CardDescription className="line-clamp-2 min-h-[2.5rem] mt-2">
              {credential.notes}
            </CardDescription>
          )}
        </CardHeader>

        <CardContent className="pt-0 flex-1 min-h-0">
          <div className="flex items-center gap-2 flex-wrap">
            <Badge variant="secondary">
              {getCredentialTypeLabel(credential.type)}
            </Badge>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Badge variant="outline" className="gap-1 bg-blue-50 text-blue-700 border-blue-200">
                    <Users className="h-3 w-3" />
                    Shared
                  </Badge>
                </TooltipTrigger>
                <TooltipContent>
                  <p>Shared by {credential.owner_email}</p>
                  <p className="text-xs text-muted-foreground">
                    {new Date(credential.shared_at).toLocaleDateString()}
                  </p>
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          </div>
          <div className="mt-2 text-xs text-muted-foreground">
            Shared by {credential.owner_email}
          </div>
        </CardContent>
      </Card>
    </Link>
  )
}

export function SharedWithMeCredentials() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["credentials-shared-with-me"],
    queryFn: () => CredentialsService.getCredentialsSharedWithMe(),
  })

  if (isLoading) {
    return null // Don't show loading state to avoid layout shift
  }

  if (error) {
    return null // Don't show errors, fail silently
  }

  const credentials = data?.data || []

  if (credentials.length === 0) {
    return null // Don't show section if no shared credentials
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Users className="h-5 w-5 text-blue-500" />
        <h2 className="text-lg font-semibold">Shared with Me</h2>
        <Badge variant="secondary" className="ml-1">
          {credentials.length}
        </Badge>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 auto-rows-fr">
        {credentials.map((credential) => (
          <SharedCredentialCard key={credential.id} credential={credential} />
        ))}
      </div>
    </div>
  )
}
