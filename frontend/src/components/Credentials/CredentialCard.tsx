import { Link } from "@tanstack/react-router"
import {
  Key,
  KeyRound,
  Mail,
  Database,
  AtSign,
  Share2,
  Users,
  AlertTriangle,
  FileJson,
  Bot,
  Package,
} from "lucide-react"

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

/**
 * Normalized view-model the card renders, covering both owned credentials
 * (CredentialPublic) and credentials shared with the current user
 * (SharedCredentialPublic). The credentials route builds this from the two
 * fetches and the server-computed ``category`` decides which tab the card
 * lands in.
 */
export interface CredentialCardModel {
  id: string
  name: string
  type: string
  notes?: string | null
  category: string
  agent_usage_count: number
  used_in_bundle: boolean
  is_shared: boolean
  // Owner-only fields (only meaningful when !is_shared).
  allow_sharing?: boolean
  share_count?: number
  status?: string | null
  // Shared-only fields (only meaningful when is_shared).
  owner_email?: string | null
  shared_at?: string | null
}

interface CredentialCardProps {
  credential: CredentialCardModel
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
    case "ssh_key":
      return <KeyRound className="h-5 w-5" />
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
    case "ssh_key":
      return "SSH Key"
    case "agent_api":
      return "Agent REST API"
    case "mcp_provider":
      return "MCP Provider"
    default:
      return type
  }
}

export function CredentialCard({ credential }: CredentialCardProps) {
  const isShared = credential.is_shared
  const shareCount = credential.share_count ?? 0
  const isIncomplete = credential.status === "incomplete"
  const agentUsageCount = credential.agent_usage_count ?? 0

  return (
    <Link
      to="/credential/$credentialId"
      params={{ credentialId: credential.id }}
      className="block h-full"
    >
      <Card className="relative transition-all hover:shadow-md hover:-translate-y-0.5 cursor-pointer h-full flex flex-col gap-0">
        <CardHeader className="pb-2">
          <div className="flex items-start gap-3">
            <div
              className={
                isShared
                  ? "rounded-lg bg-blue-500/10 p-2 text-blue-500"
                  : "rounded-lg bg-primary/10 p-2 text-primary"
              }
            >
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

            {/* Incomplete badge — owner only (we never decrypt shared creds). */}
            {!isShared && isIncomplete && (
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

            {/* Agents-using badge — both owned and shared rows. */}
            {agentUsageCount > 0 && (
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Badge variant="outline" className="gap-1">
                      <Bot className="h-3 w-3" />
                      {agentUsageCount}
                    </Badge>
                  </TooltipTrigger>
                  <TooltipContent>
                    Used by {agentUsageCount} agent{agentUsageCount > 1 ? "s" : ""}
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            )}

            {/* Bundle badge — credential is used in a bundle. */}
            {credential.used_in_bundle && (
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Badge variant="outline" className="gap-1">
                      <Package className="h-3 w-3" />
                      bundle
                    </Badge>
                  </TooltipTrigger>
                  <TooltipContent>
                    This credential is used in a bundle
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            )}

            {/* Shared treatment — blue "Shared" badge for shared-in rows. */}
            {isShared && (
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Badge
                      variant="outline"
                      className="gap-1 bg-blue-50 text-blue-700 border-blue-200"
                    >
                      <Users className="h-3 w-3" />
                      Shared
                    </Badge>
                  </TooltipTrigger>
                  <TooltipContent>
                    {credential.shared_at ? (
                      <p>
                        Shared on{" "}
                        {new Date(credential.shared_at).toLocaleDateString()}
                      </p>
                    ) : (
                      <p>Shared with you</p>
                    )}
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            )}

            {/* Owner-only shareable / share-count badge. */}
            {!isShared && credential.allow_sharing && (
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

          {isShared && credential.owner_email && (
            <div className="mt-2 text-xs text-muted-foreground">
              Shared by {credential.owner_email}
            </div>
          )}
        </CardContent>
      </Card>
    </Link>
  )
}
