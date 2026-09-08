import { useEffect, useRef } from "react"
import type { SessionCommandPublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"

interface SlashCommandPopupProps {
  commands: SessionCommandPublic[]
  selectedIndex: number
  onSelect: (command: SessionCommandPublic) => void
  filter: string
}

const MAX_TOOLTIP_COMMAND_LENGTH = 120

function truncateCommand(cmd: string): string {
  if (cmd.length <= MAX_TOOLTIP_COMMAND_LENGTH) return cmd
  return cmd.slice(0, MAX_TOOLTIP_COMMAND_LENGTH) + "…"
}

export function SlashCommandPopup({
  commands,
  selectedIndex,
  onSelect,
}: SlashCommandPopupProps) {
  const itemRefs = useRef<(HTMLDivElement | null)[]>([])

  // Scroll selected item into view when keyboard navigation changes selection
  useEffect(() => {
    if (selectedIndex >= 0 && itemRefs.current[selectedIndex]) {
      itemRefs.current[selectedIndex]?.scrollIntoView({ block: "nearest" })
    }
  }, [selectedIndex])

  if (commands.length === 0) return null

  return (
    <TooltipProvider>
      <div className="absolute bottom-full left-0 right-0 mb-1 z-50 flex justify-center">
        <div
          className="min-w-80 bg-background border border-border rounded-lg shadow-lg overflow-hidden max-h-64 overflow-y-auto"
          role="listbox"
          aria-label="Slash commands"
        >
          <table className="w-full">
            <tbody>
              {commands.map((command, index) => {
                const isSelected = index === selectedIndex
                const isUnavailable = !command.is_available
                const isDynamic = command.resolved_command != null

                const rowContent = (
                  <tr
                    key={command.name}
                    ref={(el) => { itemRefs.current[index] = el as unknown as HTMLDivElement }}
                    role="option"
                    aria-selected={isSelected}
                    aria-describedby={isDynamic ? `cmd-tooltip-${command.name}` : undefined}
                    onClick={() => !isUnavailable && onSelect(command)}
                    className={[
                      "transition-colors",
                      isUnavailable ? "opacity-50 cursor-not-allowed" : "cursor-pointer",
                      isSelected ? "bg-accent text-accent-foreground" : "",
                      !isUnavailable && !isSelected ? "hover:bg-accent/50" : "",
                    ].join(" ")}
                  >
                    <td className="px-3 py-1.5 font-mono text-sm font-medium whitespace-nowrap">
                      <span className="inline-flex items-center gap-1.5">
                        {command.name}
                        {/* Skills look like commands but are not registered
                            handlers — the text goes to the model. The badge is
                            visible text inside the `role="option"` row, so it
                            is part of the option's accessible name and needs no
                            `sr-only` twin. `h-5` keeps a badged row exactly as
                            tall as an unbadged one. */}
                        {command.kind === "skill" && (
                          <Badge
                            variant="outline"
                            className="h-5 text-[10px] font-normal"
                          >
                            skill
                          </Badge>
                        )}
                      </span>
                    </td>
                    <td className="px-3 py-1.5 text-sm text-muted-foreground">{command.description}</td>
                    {isUnavailable && (
                      <td className="px-3 py-1.5 whitespace-nowrap">
                        <span className="text-xs px-1.5 py-0.5 rounded border border-border text-muted-foreground">
                          Unavailable
                        </span>
                      </td>
                    )}
                    {/* Hidden span for screen readers on dynamic /run:<name> commands */}
                    {isDynamic && (
                      <td className="sr-only">
                        <span id={`cmd-tooltip-${command.name}`}>
                          {command.resolved_command}
                        </span>
                      </td>
                    )}
                  </tr>
                )

                if (isDynamic && command.resolved_command && !isUnavailable) {
                  return (
                    <Tooltip key={command.name}>
                      <TooltipTrigger asChild>
                        {rowContent}
                      </TooltipTrigger>
                      <TooltipContent side="right">
                        <code className="font-mono text-xs">
                          {truncateCommand(command.resolved_command)}
                        </code>
                      </TooltipContent>
                    </Tooltip>
                  )
                }

                return rowContent
              })}
            </tbody>
          </table>
        </div>
      </div>
    </TooltipProvider>
  )
}
