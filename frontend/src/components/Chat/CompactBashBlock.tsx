import { useMemo } from "react"
import { Terminal } from "lucide-react"

interface CompactBashBlockProps {
  command?: string  // Reserved for future use
}

const funnyMessages = [
  "Making some magic",
  "Doing rocket science",
  "Brewing coffee for the CPU",
  "Summoning the code spirits",
  "Consulting the crystal ball",
  "Twisting some bits",
  "Feeding the hamsters",
  "Waking up the electrons"
]

export function CompactBashBlock({ command: _command }: CompactBashBlockProps) {
  // Pick one random message and keep it static
  const randomMessage = useMemo(
    () => funnyMessages[Math.floor(Math.random() * funnyMessages.length)],
    []
  )

  return (
    <div className="inline-flex items-center gap-2 text-sm text-muted-foreground/80 mb-1">
      <Terminal className="h-3.5 w-3.5 flex-shrink-0" />
      <span>{randomMessage} ...</span>
    </div>
  )
}
