import type { ReactNode } from "react"
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react"

import type { MfaChallenge } from "@/client"

/**
 * Post-login MFA challenge state held strictly in memory. We never
 * persist the challenge_token to localStorage or sessionStorage — if the
 * tab reloads mid-challenge the user simply restarts the login flow.
 */
interface MfaChallengeContextValue {
  challenge: MfaChallenge | null
  /** Same-origin redirect target to honor after successful verification. */
  redirectTo: string | null
  setChallenge: (challenge: MfaChallenge, redirectTo: string | null) => void
  clearChallenge: () => void
}

const MfaChallengeContext = createContext<MfaChallengeContextValue | null>(null)

interface MfaChallengeProviderProps {
  children: ReactNode
}

export function MfaChallengeProvider({ children }: MfaChallengeProviderProps) {
  const [challenge, setChallengeState] = useState<MfaChallenge | null>(null)
  const [redirectTo, setRedirectTo] = useState<string | null>(null)

  const setChallenge = useCallback(
    (next: MfaChallenge, target: string | null) => {
      setChallengeState(next)
      setRedirectTo(target)
    },
    [],
  )

  const clearChallenge = useCallback(() => {
    setChallengeState(null)
    setRedirectTo(null)
  }, [])

  const value = useMemo(
    () => ({ challenge, redirectTo, setChallenge, clearChallenge }),
    [challenge, redirectTo, setChallenge, clearChallenge],
  )

  return (
    <MfaChallengeContext.Provider value={value}>
      {children}
    </MfaChallengeContext.Provider>
  )
}

export function useMfaChallenge(): MfaChallengeContextValue {
  const ctx = useContext(MfaChallengeContext)
  if (!ctx) {
    throw new Error("useMfaChallenge must be used inside MfaChallengeProvider")
  }
  return ctx
}
