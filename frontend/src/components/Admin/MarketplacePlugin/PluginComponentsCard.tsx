import {
  Blocks,
  GraduationCap,
  type LucideIcon,
  Plug,
  Terminal,
} from "lucide-react"
import { Fragment, type ReactNode } from "react"

import type { LLMPluginMarketplacePluginPublic } from "@/client"
import {
  ListRow,
  ListRowGroup,
  RowFlag,
  RowInfo,
} from "@/components/Common/ListRow"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import { mcpAuthLabel, mcpTransportLabel } from "@/utils/marketplace"
import { Fact } from "./PluginFacts"

function Tile({ icon: Icon }: { icon: LucideIcon }) {
  return (
    <span className="flex h-6 w-6 items-center justify-center rounded-md bg-muted">
      <Icon className="h-3.5 w-3.5 text-muted-foreground" />
    </span>
  )
}

function NameChips({ names }: { names: string[] }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {names.map((name) => (
        <Badge key={name} variant="outline" className="font-mono">
          {name}
        </Badge>
      ))}
    </div>
  )
}

/**
 * "Yes — <the reasons that apply>": the server's `runs_code` verdict, with
 * the kinds in this entry that make it true named rather than the generic
 * list of every possible reason.
 */
function runsCodeValue(
  runsCode: boolean,
  components: NonNullable<LLMPluginMarketplacePluginPublic["components"]>,
): string {
  if (!runsCode) return "No"
  const reasons = [
    (components.skills ?? []).some((skill) => skill.has_scripts) &&
      "skill scripts",
    (components.hooks?.events ?? []).length > 0 && "hooks",
    (components.mcp_servers ?? []).some(
      (server) => server.transport === "stdio",
    ) && "local MCP servers",
    (components.lsp_servers ?? []).length > 0 && "LSP servers",
  ].filter(Boolean)
  return reasons.length > 0 ? `Yes — ${reasons.join(", ")}` : "Yes"
}

interface Section {
  key: string
  title: string
  count: number
  body: ReactNode
}

/**
 * S3 — the full list of what this entry ships. A detail route, not a card in
 * a grid, so every list renders in full (2026-09-09 ruling).
 *
 * `no_components` is said by the Snapshot card's Alert; the caller does not
 * render this card for it.
 */
export function PluginComponentsCard({
  plugin,
}: {
  plugin: LLMPluginMarketplacePluginPublic
}) {
  const components = plugin.components ?? null
  const status = plugin.snapshot_status

  const header = (
    <CardHeader>
      <CardTitle className="flex min-w-0 items-center gap-2">
        <Blocks className="h-5 w-5" />
        Components
      </CardTitle>
      <CardDescription>What this entry adds to an agent.</CardDescription>
    </CardHeader>
  )

  if (!components) {
    return (
      <Card>
        {header}
        <CardContent>
          <p className="text-sm text-muted-foreground">
            {status === "failed"
              ? "No components — see Snapshot above."
              : "Components appear once the snapshot is ready."}
          </p>
        </CardContent>
      </Card>
    )
  }

  const skills = components.skills ?? []
  const mcp = components.mcp_servers ?? []
  const commands = components.commands ?? []
  const agents = components.agents ?? []
  const events = components.hooks?.events ?? []
  const lsp = components.lsp_servers ?? []

  const sections: Section[] = [
    {
      key: "skills",
      title: "Skills",
      count: skills.length,
      body: (
        <ListRowGroup>
          {skills.map((skill) => (
            <ListRow
              key={skill.name}
              icon={<Tile icon={GraduationCap} />}
              title={skill.name}
              flags={
                skill.has_scripts ? (
                  <RowFlag icon={Terminal} label="Ships scripts" />
                ) : undefined
              }
            />
          ))}
        </ListRowGroup>
      ),
    },
    {
      key: "mcp",
      title: "MCP servers",
      count: mcp.length,
      body: (
        <ListRowGroup>
          {mcp.map((server) => {
            const meta = [server.runtime, mcpAuthLabel(server.auth)]
              .filter(Boolean)
              .join(" · ")
            return (
              <ListRow
                key={server.name}
                icon={<Tile icon={Plug} />}
                title={server.name}
                badges={
                  <Badge variant="outline" className="h-5 shrink-0">
                    {mcpTransportLabel(server.transport)}
                  </Badge>
                }
                meta={meta || undefined}
                flags={
                  <RowInfo
                    facts={(server.env ?? []).map((env) =>
                      env.secret ? `${env.name} (secret)` : env.name,
                    )}
                  />
                }
              />
            )
          })}
        </ListRowGroup>
      ),
    },
    {
      key: "commands",
      title: "Commands",
      count: commands.length,
      body: <NameChips names={commands} />,
    },
    {
      key: "agents",
      title: "Agents",
      count: agents.length,
      body: <NameChips names={agents} />,
    },
    {
      key: "hooks",
      title: "Hooks",
      count: events.length,
      body: <NameChips names={events} />,
    },
    {
      key: "lsp",
      title: "LSP servers",
      count: lsp.length,
      body: <NameChips names={lsp} />,
    },
  ].filter((section) => section.count > 0)

  return (
    <Card>
      {header}
      <CardContent className="space-y-4">
        {sections.map((section) => (
          <Fragment key={section.key}>
            <section className="space-y-2">
              <h3 className="text-sm font-medium">
                {section.title} · {section.count}
              </h3>
              {section.body}
            </section>
            <Separator />
          </Fragment>
        ))}
        <Fact label="Runs code">
          {runsCodeValue(plugin.runs_code === true, components)}
        </Fact>
      </CardContent>
    </Card>
  )
}
