import { Link } from "@tanstack/react-router"
import { Key, Mail, Database, AtSign } from "lucide-react"

import type { CredentialPublic } from "@/client"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"

interface CredentialCardProps {
  credential: CredentialPublic
}

function getCredentialIcon(type: string) {
  switch (type) {
    case "email_imap":
      return <Mail className="h-5 w-5" />
    case "odoo":
      return <Database className="h-5 w-5" />
    case "gmail_oauth":
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
    case "odoo":
      return "Odoo"
    case "gmail_oauth":
      return "Gmail OAuth"
    case "api_token":
      return "API Token"
    default:
      return type
  }
}

export function CredentialCard({ credential }: CredentialCardProps) {
  return (
    <Card className="relative transition-all hover:shadow-md hover:-translate-y-0.5">
      <Link
        to="/credential/$credentialId"
        params={{ credentialId: credential.id }}
        className="block"
      >
        <CardHeader className="pb-3">
          <div className="flex items-start gap-3 mb-2">
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
            <CardDescription className="line-clamp-2 min-h-[2.5rem]">
              {credential.notes}
            </CardDescription>
          )}
        </CardHeader>

        <CardContent>
          <div className="flex items-center gap-2">
            <Badge variant="secondary">
              {getCredentialTypeLabel(credential.type)}
            </Badge>
          </div>
        </CardContent>
      </Link>
    </Card>
  )
}
