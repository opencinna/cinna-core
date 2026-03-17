import { Link } from "@tanstack/react-router"
import { Key, Mail, Database, AtSign, Share2, Users, AlertTriangle, FileJson } from "lucide-react"

import type { CredentialPublic } from "@/client"
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

interface CredentialCardProps {
  credential: CredentialPublic
}

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
    case "google_service_account":
      return <FileJson className="h-5 w-5" />
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
    case "google_service_account":
      return "Google Service Account"
    case "api_token":
      return "API Token"
    default:
      return type
  }
}

export function CredentialCard({ credential }: CredentialCardProps) {
  const shareCount = credential.share_count ?? 0
  const isIncomplete = credential.status === "incomplete"

  return (
    <Link
      to="/credential/$credentialId"
      params={{ credentialId: credential.id }}
      className="block h-full"
    >
      <Card className="relative transition-all hover:shadow-md hover:-translate-y-0.5 cursor-pointer h-full flex flex-col gap-0">
        <CardHeader className="pb-2">
          <div className="flex items-start gap-3">
            <div className="rounded-lg bg-primary/10 p-2 text-primary">
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
            {isIncomplete && (
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Badge variant="destructive" className="gap-1">
                      <AlertTriangle className="h-3 w-3" />
                      Incomplete
                    </Badge>
                  </TooltipTrigger>
                  <TooltipContent>
                    This credential is missing required configuration. Click to complete setup.
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            )}
            {credential.allow_sharing && (
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Badge variant="outline" className="gap-1">
                      {shareCount > 0 ? (
                        <>
                          <Users className="h-3 w-3" />
                          {shareCount}
                        </>
                      ) : (
                        <>
                          <Share2 className="h-3 w-3" />
                          Shareable
                        </>
                      )}
                    </Badge>
                  </TooltipTrigger>
                  <TooltipContent>
                    {shareCount > 0
                      ? `Shared with ${shareCount} user${shareCount > 1 ? "s" : ""}`
                      : "This credential can be shared with others"}
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            )}
          </div>
        </CardContent>
      </Card>
    </Link>
  )
}
