import { useEffect, useRef } from "react"
import type { SessionCommandPublic } from "@/client"

interface SlashCommandPopupProps {
  commands: SessionCommandPublic[]
  selectedIndex: number
  onSelect: (command: SessionCommandPublic) => void
  filter: string
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

              return (
                <tr
                  key={command.name}
                  ref={(el) => { itemRefs.current[index] = el as unknown as HTMLDivElement }}
                  role="option"
                  aria-selected={isSelected}
                  onClick={() => !isUnavailable && onSelect(command)}
                  className={[
                    "transition-colors",
                    isUnavailable ? "opacity-50 cursor-not-allowed" : "cursor-pointer",
                    isSelected ? "bg-accent text-accent-foreground" : "",
                    !isUnavailable && !isSelected ? "hover:bg-accent/50" : "",
                  ].join(" ")}
                >
                  <td className="px-3 py-1.5 font-mono text-sm font-medium whitespace-nowrap">{command.name}</td>
                  <td className="px-3 py-1.5 text-sm text-muted-foreground">{command.description}</td>
                  {isUnavailable && (
                    <td className="px-3 py-1.5 whitespace-nowrap">
                      <span className="text-xs px-1.5 py-0.5 rounded border border-border text-muted-foreground">
                        Unavailable
                      </span>
                    </td>
                  )}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
