import { useState, useEffect, useMemo } from "react"

interface RotatingHintsProps {
  hints?: string[]
  interval?: number
  className?: string
}

const defaultHints = [
  "Press Enter to send, Shift+Enter for new line",
  "Switch to building mode for code writing capabilities",
  "Select an agent to start a conversation or create a new one",
  "Use Shift+Enter to add multiple lines to your message",
  "Building mode enables advanced code generation features",
  "Conversation mode is significantly cheaper, so use it for everyday communication",
]

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
}: RotatingHintsProps) {
  // Shuffle hints once when component loads or hints prop changes
  const shuffledHints = useMemo(() => shuffleArray(hints), [hints])

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
    <p
      className={`text-xs text-muted-foreground/70 transition-opacity duration-[600ms] ${
        isVisible ? "opacity-100" : "opacity-0"
      } ${className}`}
    >
      {shuffledHints[currentIndex]}
    </p>
  )
}
