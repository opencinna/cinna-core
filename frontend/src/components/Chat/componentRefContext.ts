import { createContext, useContext } from "react"

/**
 * What a component reference card needs from the chat it sits in.
 *
 * - `sessionId` lets `component-refs/resolve` try the chat's own agent first
 *   (and resolve `id:`/slug references, which match only that agent).
 * - `seedComposer` is **Ask to fix**: it fills that chat's composer and never
 *   sends. A surface without a composer leaves it out, and the card then
 *   shows no Ask to fix.
 */
export interface ComponentRefChat {
  sessionId?: string | null
  seedComposer?: (text: string) => void
}

/** `null` outside a chat that wants cards (guest pages, documents): no cards. */
export const ComponentRefChatContext = createContext<ComponentRefChat | null>(
  null,
)

export function useComponentRefChat(): ComponentRefChat {
  return useContext(ComponentRefChatContext) ?? {}
}
