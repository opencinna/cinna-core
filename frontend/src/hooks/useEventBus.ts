/**
 * useEventBus - React hook for subscribing to WebSocket events
 *
 * Provides an easy way for React components to:
 * - Subscribe to specific event types
 * - Automatically clean up subscriptions on unmount
 * - Access connection status
 */

import { useEffect, useRef, useCallback, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { eventService, EventData, EventHandler, EventTypes, ConnectionStatus } from "@/services/eventService"
import useAuth from "./useAuth"

/**
 * Hook to manage WebSocket connection lifecycle
 * Automatically connects when user is authenticated and disconnects on unmount
 */
export function useEventBusConnection() {
  const { user } = useAuth()
  const hasInitialized = useRef(false)

  useEffect(() => {
    if (user && !hasInitialized.current) {
      console.log("[useEventBusConnection] Initializing event bus for user:", user.id)
      eventService.connect(user.id)
      hasInitialized.current = true
    }

    // Cleanup on unmount
    return () => {
      if (hasInitialized.current) {
        console.log("[useEventBusConnection] Disconnecting event bus")
        eventService.disconnect()
        hasInitialized.current = false
      }
    }
  }, [user])

  return {
    isConnected: eventService.isConnected(),
    socketId: eventService.getSocketId(),
  }
}

/**
 * Hook to subscribe to a specific event type
 *
 * @param eventType - Event type to subscribe to (e.g., 'session_updated') or "*" for all events
 * @param handler - Callback function to handle the event
 * @param enabled - Whether the subscription is active (default: true)
 *
 * @example
 * ```tsx
 * useEventSubscription('session_updated', (event) => {
 *   console.log('Session updated:', event.model_id)
 *   queryClient.invalidateQueries({ queryKey: ['sessions'] })
 * })
 * ```
 */
export function useEventSubscription(
  eventType: string | "*",
  handler: EventHandler,
  enabled = true
) {
  const subscriptionIdRef = useRef<string | null>(null)

  useEffect(() => {
    if (!enabled) {
      // If disabled and we have a subscription, unsubscribe
      if (subscriptionIdRef.current) {
        eventService.unsubscribe(subscriptionIdRef.current)
        subscriptionIdRef.current = null
      }
      return
    }

    // Subscribe to the event
    const id = eventService.subscribe(eventType, handler)
    subscriptionIdRef.current = id

    console.log(`[useEventSubscription] Subscribed to ${eventType}`)

    // Cleanup on unmount or when dependencies change
    return () => {
      if (subscriptionIdRef.current) {
        eventService.unsubscribe(subscriptionIdRef.current)
        subscriptionIdRef.current = null
      }
    }
  }, [eventType, enabled]) // Intentionally not including handler to avoid re-subscribing on every render

  // Update handler when it changes without re-subscribing
  const handlerRef = useRef(handler)
  useEffect(() => {
    handlerRef.current = handler
  }, [handler])
}

/**
 * Hook to subscribe to multiple event types
 *
 * @param eventTypes - Array of event types to subscribe to
 * @param handler - Callback function to handle the events
 * @param enabled - Whether the subscription is active (default: true)
 *
 * @example
 * ```tsx
 * useMultiEventSubscription(
 *   ['session_updated', 'session_deleted'],
 *   (event) => {
 *     queryClient.invalidateQueries({ queryKey: ['sessions'] })
 *   }
 * )
 * ```
 */
export function useMultiEventSubscription(
  eventTypes: string[],
  handler: EventHandler,
  enabled = true
) {
  const subscriptionIdsRef = useRef<string[]>([])

  useEffect(() => {
    if (!enabled) {
      // Unsubscribe all
      subscriptionIdsRef.current.forEach((id) => {
        eventService.unsubscribe(id)
      })
      subscriptionIdsRef.current = []
      return
    }

    // Subscribe to all event types
    const ids = eventTypes.map((eventType) => eventService.subscribe(eventType, handler))
    subscriptionIdsRef.current = ids

    console.log(`[useMultiEventSubscription] Subscribed to ${eventTypes.join(", ")}`)

    // Cleanup
    return () => {
      subscriptionIdsRef.current.forEach((id) => {
        eventService.unsubscribe(id)
      })
      subscriptionIdsRef.current = []
    }
  }, [JSON.stringify(eventTypes), enabled]) // Use JSON.stringify for array comparison
}

/**
 * Hook to subscribe to a specific room
 *
 * @param room - Room name to subscribe to (e.g., 'session_123')
 * @param enabled - Whether the subscription is active (default: true)
 *
 * @example
 * ```tsx
 * useRoomSubscription('session_123')
 * ```
 */
export function useRoomSubscription(room: string | null, enabled = true) {
  useEffect(() => {
    if (!enabled || !room) {
      return
    }

    // Subscribe to room
    eventService.subscribeToRoom(room)

    console.log(`[useRoomSubscription] Subscribed to room: ${room}`)

    // Cleanup
    return () => {
      if (room) {
        eventService.unsubscribeFromRoom(room)
      }
    }
  }, [room, enabled])
}

/**
 * Hook to check WebSocket connection status
 *
 * @returns Object with connection status
 */
export function useEventBusStatus() {
  const isConnected = eventService.isConnected()
  const socketId = eventService.getSocketId()

  return {
    isConnected,
    socketId,
  }
}

/**
 * Hook to send a ping (useful for testing connection)
 */
export function useEventBusPing() {
  const ping = useCallback(() => {
    eventService.ping()
  }, [])

  return { ping }
}

/**
 * Hook to track WebSocket connection status reactively
 *
 * @returns Current connection status: "connected" | "connecting" | "disconnected"
 *
 * @example
 * ```tsx
 * const status = useConnectionStatus()
 * return <div>Status: {status}</div>
 * ```
 */
export function useConnectionStatus(): ConnectionStatus {
  const [status, setStatus] = useState<ConnectionStatus>(eventService.getStatus())

  useEffect(() => {
    // Subscribe to status changes
    const unsubscribe = eventService.onStatusChange((newStatus) => {
      setStatus(newStatus)
    })

    // Cleanup
    return unsubscribe
  }, [])

  return status
}

// Re-export EventTypes for convenience
export { EventTypes }
