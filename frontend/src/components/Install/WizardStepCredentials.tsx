/**
 * Step 2 — Credentials.
 *
 * For each ``required_credential_specs`` entry the user can either pick
 * one of their existing credentials of the matching type, or leave the
 * dropdown on "Create placeholder" so the platform creates an empty
 * credential to fill in later (matches the legacy accept-share UX).
 */
import { useQuery } from "@tanstack/react-query"

import { CredentialsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

export interface CredentialSelection {
  selectedCredentialId: string | null // existing UUID or "__create_placeholder__"
}

interface WizardStepCredentialsProps {
  requiredSpecs: Array<{ name: string; type: string; allow_sharing?: boolean }>
  selections: Record<string, CredentialSelection>
  onChange: (next: Record<string, CredentialSelection>) => void
}

export function WizardStepCredentials({
  requiredSpecs,
  selections,
  onChange,
}: WizardStepCredentialsProps) {
  const { data: credentials } = useQuery({
    queryKey: ["credentials"],
    queryFn: () => CredentialsService.readCredentials(),
  })

  const allCredentials = credentials?.data ?? []

  const updateSelection = (specName: string, value: string) => {
    onChange({
      ...selections,
      [specName]: { selectedCredentialId: value },
    })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Credentials</CardTitle>
        <CardDescription>
          Pick one of your existing credentials for each requirement, or leave
          on "Create placeholder" to fill in later.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {requiredSpecs.map((spec) => {
          const matching = allCredentials.filter((c) => {
            const credType =
              typeof c.type === "string" ? c.type : (c.type as any).value
            return credType === spec.type
          })
          const current =
            selections[spec.name]?.selectedCredentialId ??
            "__create_placeholder__"

          return (
            <div key={spec.name} className="space-y-1.5">
              <Label className="flex items-center gap-2">
                <span>{spec.name}</span>
                <Badge variant="secondary" className="text-xs font-normal">
                  {spec.type}
                </Badge>
              </Label>
              <Select
                value={current}
                onValueChange={(val) => updateSelection(spec.name, val)}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Choose credential or create placeholder" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__create_placeholder__">
                    Create placeholder (fill in later)
                  </SelectItem>
                  {matching.length > 0 && (
                    <>
                      {matching.map((c) => (
                        <SelectItem key={c.id} value={c.id}>
                          {c.name}
                        </SelectItem>
                      ))}
                    </>
                  )}
                </SelectContent>
              </Select>
              {matching.length === 0 && (
                <p className="text-xs text-muted-foreground">
                  You don't have any credentials of type{" "}
                  <code className="font-mono">{spec.type}</code> yet — a
                  placeholder will be created so you can fill it in after
                  install.
                </p>
              )}
            </div>
          )
        })}
      </CardContent>
    </Card>
  )
}
