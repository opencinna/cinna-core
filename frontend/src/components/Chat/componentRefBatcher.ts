import { type ComponentRefResolved, DashboardChatService } from "@/client"

/** The resolve route's own limit (`component-refs/resolve`, ≤ 20 refs). */
const MAX_REFS_PER_REQUEST = 20

interface Waiter {
  ref: string
  resolve: (result: ComponentRefResolved | null) => void
  reject: (error: unknown) => void
}

const queues = new Map<string, Waiter[]>()

function flush(key: string, sessionId: string | null) {
  const waiters = queues.get(key) ?? []
  queues.delete(key)
  for (let i = 0; i < waiters.length; i += MAX_REFS_PER_REQUEST) {
    const chunk = waiters.slice(i, i + MAX_REFS_PER_REQUEST)
    const refs = Array.from(new Set(chunk.map((w) => w.ref)))
    DashboardChatService.resolveComponentRefs({
      requestBody: { refs, session_id: sessionId },
    }).then(
      ({ results }) => {
        const byRef = new Map(results.map((r) => [r.ref, r]))
        for (const w of chunk) w.resolve(byRef.get(w.ref) ?? null)
      },
      (error) => {
        for (const w of chunk) w.reject(error)
      },
    )
  }
}

/**
 * Resolve one reference, batched with every other card that asks in the same
 * tick for the same chat session: one request per 20 references instead of one
 * per card.
 */
export function resolveComponentRef(
  ref: string,
  sessionId: string | null,
): Promise<ComponentRefResolved | null> {
  const key = sessionId ?? ""
  return new Promise((resolve, reject) => {
    let queue = queues.get(key)
    if (!queue) {
      queue = []
      queues.set(key, queue)
      queueMicrotask(() => flush(key, sessionId))
    }
    queue.push({ ref, resolve, reject })
  })
}
