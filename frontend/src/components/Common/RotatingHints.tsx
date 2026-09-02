import { useState, useEffect, useMemo } from "react"

import { useLocalAgentKitAvailable } from "@/hooks/useLocalAgentKit"

interface RotatingHintsProps {
  hints?: string[]
  interval?: number
  className?: string
  onClick?: () => void
}

const defaultHints = [
  "Press Enter to send, Shift+Enter for new line",
  "Switch to building mode for code writing capabilities",
  "Select an agent to start a conversation or create a new one",
  "Use Shift+Enter to add multiple lines to your message",
  "Building mode enables advanced code generation features",
  "Conversation mode is significantly cheaper, so use it for everyday communication",
  "Agents with entrypoint prompts auto-fill the input field when selected",
  "Click the selected agent badge to toggle between empty input and default prompt",
  "Edit the input to switch to manual mode and keep your custom message",
  "You can drag-n-drop files to the message input area to send them to the agent",
]

// Hints that are only true on some instances. Appended to whatever hint list
// the caller passed, so no call site has to know the surface exists.
const LOCAL_AGENT_KIT_HINT =
  "Already use Claude Code or Codex? You can build agents locally first — see Getting Started"

// Fisher-Yates shuffle algorithm
function shuffleArray<T>(array: T[]): T[] {
  const shuffled = [...array]
  for (let i = shuffled.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    ;[shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]]
  }
  return shuffled
}

export function RotatingHints({
  hints = defaultHints,
  interval = 8000,
  className = "",
  onClick,
}: RotatingHintsProps) {
  // The local-first hint points at `/agent-start`, which 404s on an instance that
  // switched the public kit off — so it is conditional, and conditional here
  // rather than at each call site because every caller wants the same answer.
  // The probe is one cached query per session, shared with the other surfaces.
  const localAgentKitAvailable = useLocalAgentKitAvailable()

  // Shuffle hints once when component loads or hints prop changes
  const shuffledHints = useMemo(
    () =>
      shuffleArray(
        localAgentKitAvailable ? [...hints, LOCAL_AGENT_KIT_HINT] : hints,
      ),
    [hints, localAgentKitAvailable],
  )

  const [currentIndex, setCurrentIndex] = useState(0)
  const [isVisible, setIsVisible] = useState(true)

  useEffect(() => {
    if (shuffledHints.length <= 1) return

    const timer = setInterval(() => {
      // Fade out
      setIsVisible(false)

      // Wait for fade out, then change text and fade in
      setTimeout(() => {
        setCurrentIndex((prev) => (prev + 1) % shuffledHints.length)
        setIsVisible(true)
      }, 300) // Half of the transition duration
    }, interval)

    return () => clearInterval(timer)
  }, [shuffledHints.length, interval])

  return (
    <button
      type="button"
      onClick={onClick}
      className={`text-xs text-muted-foreground/70 transition-all duration-[600ms] text-left hover:text-muted-foreground cursor-pointer ${
        isVisible ? "opacity-100" : "opacity-0"
      } ${className}`}
    >
      {shuffledHints[currentIndex]}
    </button>
  )
}
