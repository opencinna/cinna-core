import { ShieldCheck } from "lucide-react"
import type { ReactNode } from "react"

import type { AgentApiKeyHolderInfo } from "@/client"
import { AgentBadge } from "@/components/Common/AgentBadge"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import useAuth from "@/hooks/useAuth"
import { pluralize } from "@/utils/pluralize"

/** Linked agents shown as badges before "+N more". */
const LINKED_AGENTS_SHOWN = 5

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <span className="shrink-0 pt-0.5 text-xs text-muted-foreground">
        {label}
      </span>
      <div className="min-w-0 text-right text-sm">{children}</div>
    </div>
  )
}

interface AgentApiKeyAccessCardProps {
  holder: AgentApiKeyHolderInfo
}

/**
 * "Access" — what the issuer decided for this key, read-only for its holder:
 * which agent it calls, who issued it, the scopes it carries, whether it can
 * write, and the key's own clock (expiry, last use, rotation). Scopes are set
 * by the issuer on the person's grant, so they are facts here, not an editor.
 *
 * The linked-agents line is the D14 risk made visible: an agent others can run
 * calls the producer *as the holder*, so the holder sees which agents carry
 * the key and is told so in words.
 */
export function AgentApiKeyAccessCard({ holder }: AgentApiKeyAccessCardProps) {
  const { user } = useAuth()
  const producer = holder.producer
  const issuer = holder.issuer
  const scopes = holder.scopes ?? []
  const linked = holder.linked_agents ?? []
  const producerName = producer?.name || "the producer"

  // The producer's page is owner-only. The issuer owns the producer, so the
  // badge links only when the holder issued the key to themself — for anyone
  // else the link would land on a page they cannot open.
  const canOpenProducer = !!user && !!issuer && issuer.id === user.id

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 min-w-0">
          <ShieldCheck className="h-5 w-5" />
          Access
        </CardTitle>
        <CardDescription>Set by the issuer; read-only here.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Fact label="Producer">
          {producer ? (
            <div className="flex justify-end">
              <AgentBadge
                agent={{
                  id: producer.id,
                  name: producer.name,
                  ui_color_preset: producer.ui_color_preset,
                }}
                linkTo={canOpenProducer ? "agent" : "none"}
              />
            </div>
          ) : (
            "Deleted"
          )}
        </Fact>

        <Fact label="Issued by">
          {issuer ? (
            <>
              <div className="truncate font-medium">
                {issuer.full_name || issuer.email || "Unknown user"}
              </div>
              {issuer.full_name && issuer.email && (
                <div className="truncate text-xs text-muted-foreground">
                  {issuer.email}
                </div>
              )}
            </>
          ) : (
            "Account removed"
          )}
        </Fact>

        <Fact label="Scopes">
          {scopes.length === 0 ? (
            "No scopes"
          ) : (
            <div className="space-y-1">
              <div>{pluralize(scopes.length, "scope", "scopes")}</div>
              <div className="flex flex-wrap justify-end gap-1">
                {scopes.map((scope) => (
                  <Badge
                    key={scope}
                    variant="secondary"
                    className="h-5 font-mono text-xs"
                  >
                    {scope}
                  </Badge>
                ))}
              </div>
            </div>
          )}
        </Fact>

        <Fact label="Access">
          {holder.read_only ? "Read-only" : "Read and write"}
        </Fact>

        <Fact label="Expires">
          {holder.expires_at ? (
            <RelativeTime timestamp={holder.expires_at} showTooltip />
          ) : (
            "Never"
          )}
        </Fact>

        <Fact label="Last used">
          {holder.last_used_at ? (
            <RelativeTime timestamp={holder.last_used_at} showTooltip />
          ) : (
            "Never"
          )}
        </Fact>

        <Fact label="Rotated">
          {holder.rotated_at ? (
            <RelativeTime timestamp={holder.rotated_at} showTooltip />
          ) : (
            "Never"
          )}
        </Fact>

        <Fact label="Linked agents">
          {linked.length === 0 ? (
            "No agents"
          ) : (
            <div className="space-y-1">
              <div>{pluralize(linked.length, "agent", "agents")}</div>
              <div className="flex flex-wrap justify-end gap-1">
                {linked.slice(0, LINKED_AGENTS_SHOWN).map((agent) => (
                  <AgentBadge
                    key={agent.id}
                    agent={{
                      id: agent.id,
                      name: agent.name,
                      ui_color_preset: agent.ui_color_preset,
                    }}
                    linkTo="agent"
                  />
                ))}
                {linked.length > LINKED_AGENTS_SHOWN && (
                  <span className="text-xs text-muted-foreground">
                    +{linked.length - LINKED_AGENTS_SHOWN} more
                  </span>
                )}
              </div>
            </div>
          )}
        </Fact>

        {linked.length > 0 && (
          <p className="text-xs text-muted-foreground">
            Anyone who can run these agents can call {producerName} as you.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
