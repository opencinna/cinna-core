/**
 * CredentialTypeBadge — display-only badge for a credential type, sharing
 * the icon + colour palette with the "Add Credential" picker.
 */
import type { CredentialType } from "@/client"
import { cn } from "@/lib/utils"
import { getCredentialTypeMeta } from "@/components/Credentials/credentialTypes"

interface CredentialTypeBadgeProps {
  type: CredentialType | string
  className?: string
}

export function CredentialTypeBadge({ type, className }: CredentialTypeBadgeProps) {
  const meta = getCredentialTypeMeta(type)
  const Icon = meta.icon
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm font-medium",
        meta.badgeClass,
        className,
      )}
    >
      <Icon className="h-3.5 w-3.5 shrink-0" />
      <span>{meta.label}</span>
    </span>
  )
}
