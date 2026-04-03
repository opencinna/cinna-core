/**
 * EventService - WebSocket client for real-time event communication
 *
 * Provides a singleton Socket.IO client that connects to the backend
 * WebSocket server for real-time event updates.
 */

import { io, Socket } from "socket.io-client"

// Event types matching backend EventType
export const EventTypes = {
  // Session events
  SESSION_CREATED: "session_created",
  SESSION_UPDATED: "session_updated",
  SESSION_DELETED: "session_deleted",

  // Message events
  MESSAGE_CREATED: "message_created",
  MESSAGE_UPDATED: "message_updated",
  MESSAGE_DELETED: "message_deleted",

  // Activity events
  ACTIVITY_CREATED: "activity_created",
  ACTIVITY_UPDATED: "activity_updated",
  ACTIVITY_DELETED: "activity_deleted",

  // Agent events
  AGENT_CREATED: "agent_created",
  AGENT_UPDATED: "agent_updated",
  AGENT_DELETED: "agent_deleted",

  // Environment events
  ENVIRONMENT_ACTIVATING: "environment_activating",
  ENVIRONMENT_ACTIVATED: "environment_activated",
  ENVIRONMENT_ACTIVATION_FAILED: "environment_activation_failed",
  ENVIRONMENT_SUSPENDED: "environment_suspended",
  ENVIRONMENT_STATUS_CHANGED: "environment_status_changed",

  // Streaming events
  STREAM_STARTED: "stream_started",
  STREAM_COMPLETED: "stream_completed",
  STREAM_ERROR: "stream_error",

  // Session streaming status change (DB-based, emitted on interaction_status change)
  SESSION_INTERACTION_STATUS_CHANGED: "session_interaction_status_changed",

  // Session state events
  SESSION_STATE_UPDATED: "session_state_updated",  // Agent declared session outcome

  // To-do progress events (from TodoWrite tool)
  TODO_LIST_UPDATED: "todo_list_updated",    // Session-level to-do update
  TASK_TODO_UPDATED: "task_todo_updated",    // Task-level to-do update

  // Task lifecycle events
  TASK_CREATED: "task_created",
  TASK_STATUS_UPDATED: "task_status_changed",

  // Task collaboration events
  // Note: TASK_STATUS_CHANGED is an alias for TASK_STATUS_UPDATED (same wire value).
  // Prefer TASK_STATUS_CHANGED for new code. TASK_STATUS_UPDATED kept for back-compat.
  TASK_COMMENT_ADDED: "task_comment_added",
  TASK_STATUS_CHANGED: "task_status_changed",
  TASK_ATTACHMENT_ADDED: "task_attachment_added",
  SUBTASK_COMPLETED: "subtask_completed",
  TASK_SUBTASK_CREATED: "task_subtask_created",

  // Generic notification
  NOTIFICATION: "notification",
} as const

export type EventType = (typeof EventTypes)[keyof typeof EventTypes]

// Event structure matching backend EventPublic
export interface EventData {
  type: string
  model_id?: string
  text_content?: string
  meta?: Record<string, any>
  user_id?: string
  timestamp: string
}

// Event handler callback type
export type EventHandler = (event: EventData) => void

// Event subscription
interface EventSubscription {
  eventType: string | "*" // "*" means subscribe to all events
  handler: EventHandler
  id: string
}

// Connection status
export type ConnectionStatus = "connected" | "connecting" | "disconnected"

// Connection status listener
export type ConnectionStatusListener = (status: ConnectionStatus) => void

class EventServiceClass {
  private socket: Socket | null = null
  private subscriptions: Map<string, EventSubscription> = new Map()
  private activeRooms: Set<string> = new Set()
  private isConnecting = false
  private reconnectAttempts = 0
  private maxReconnectAttempts = 5
  private reconnectDelay = 1000 // Start with 1 second
  private statusListeners: Set<ConnectionStatusListener> = new Set()
  private currentStatus: ConnectionStatus = "disconnected"

  /**
   * Initialize the WebSocket connection
   */
  connect(userId: string): void {
    if (this.socket?.connected || this.isConnecting) {
      console.log("[EventService] Already connected or connecting")
      return
    }

    this.isConnecting = true
    this.updateStatus("connecting")
    const apiUrl = import.meta.env.VITE_API_URL || "http://localhost:8000"

    console.log("[EventService] Connecting to:", apiUrl, "with path: /ws")

    this.socket = io(apiUrl, {
      path: "/ws",
      transports: ["websocket", "polling"],
      auth: {
        user_id: userId,
      },
      reconnection: true,
      reconnectionAttempts: this.maxReconnectAttempts,
      reconnectionDelay: this.reconnectDelay,
      reconnectionDelayMax: 5000,
    })

    // Connection event handlers
    this.socket.on("connect", () => {
      console.log("[EventService] Connected, socket ID:", this.socket?.id)
      this.isConnecting = false
      this.reconnectAttempts = 0
      this.reconnectDelay = 1000
      this.updateStatus("connected")

      // Re-subscribe to any active rooms after reconnect
      if (this.activeRooms.size > 0) {
        this.activeRooms.forEach((room) => {
          console.log("[EventService] Re-subscribing to room:", room)
          this.socket?.emit("subscribe", { room }, (response: any) => {
            if (response?.status === "success") {
              console.log(`[EventService] Re-subscribed to room: ${room}`)
            } else {
              console.error(`[EventService] Failed to re-subscribe to room: ${room}`, response)
            }
          })
        })
      }
    })

    this.socket.on("disconnect", (reason) => {
      console.log("[EventService] Disconnected:", reason)
      this.isConnecting = false
      this.updateStatus("disconnected")
    })

    this.socket.on("connect_error", (error) => {
      console.error("[EventService] Connection error:", error)
      this.isConnecting = false
      this.reconnectAttempts++

      // Exponential backoff
      if (this.reconnectAttempts < this.maxReconnectAttempts) {
        this.reconnectDelay = Math.min(this.reconnectDelay * 2, 5000)
        this.updateStatus("connecting")
      } else {
        this.updateStatus("disconnected")
      }
    })

    // Listen for events from server
    this.socket.on("event", (data: EventData) => {
      console.log("[EventService] Received event:", data.type, data)
      this.handleEvent(data)
    })

    // Listen for streaming events
    this.socket.on("stream_event", (data: any) => {
      console.log("[EventService] Received stream event:", data.event_type, data)
      // Treat stream_event as a special event type for subscribers
      this.handleStreamEvent(data)
    })

    // Ping/pong for keepalive
    this.socket.on("pong", (data) => {
      console.log("[EventService] Pong received:", data)
    })
  }

  /**
   * Disconnect from WebSocket
   */
  disconnect(): void {
    if (this.socket) {
      console.log("[EventService] Disconnecting...")
      this.socket.disconnect()
      this.socket = null
      this.isConnecting = false
      this.subscriptions.clear()
      this.activeRooms.clear()
      this.updateStatus("disconnected")
    }
  }

  /**
   * Subscribe to specific event type or all events
   * @param eventType - Event type to subscribe to, or "*" for all events
   * @param handler - Callback function to handle the event
   * @returns Subscription ID (use to unsubscribe)
   */
  subscribe(eventType: string | "*", handler: EventHandler): string {
    const id = `${eventType}_${Date.now()}_${Math.random()}`
    this.subscriptions.set(id, { eventType, handler, id })
    console.log(`[EventService] Subscribed to ${eventType}, subscription ID:`, id)
    return id
  }

  /**
   * Unsubscribe from an event
   * @param subscriptionId - ID returned from subscribe()
   */
  unsubscribe(subscriptionId: string): void {
    const subscription = this.subscriptions.get(subscriptionId)
    if (subscription) {
      this.subscriptions.delete(subscriptionId)
      console.log(`[EventService] Unsubscribed from ${subscription.eventType}`)
    }
  }

  /**
   * Subscribe to a specific room
   * @param room - Room name to subscribe to
   */
  async subscribeToRoom(room: string): Promise<void> {
    // Track room regardless of connection state so it gets re-subscribed on reconnect
    this.activeRooms.add(room)

    if (!this.socket) {
      console.warn("[EventService] Cannot subscribe to room: not connected (will subscribe on reconnect)")
      return
    }

    return new Promise((resolve, reject) => {
      this.socket?.emit("subscribe", { room }, (response: any) => {
        if (response?.status === "success") {
          console.log(`[EventService] Subscribed to room: ${room}`)
          resolve()
        } else {
          console.error(`[EventService] Failed to subscribe to room: ${room}`, response)
          reject(new Error(response?.message || "Failed to subscribe"))
        }
      })
    })
  }

  /**
   * Unsubscribe from a specific room
   * @param room - Room name to unsubscribe from
   */
  async unsubscribeFromRoom(room: string): Promise<void> {
    this.activeRooms.delete(room)

    if (!this.socket) {
      console.warn("[EventService] Cannot unsubscribe from room: not connected")
      return
    }

    return new Promise((resolve, reject) => {
      this.socket?.emit("unsubscribe", { room }, (response: any) => {
        if (response?.status === "success") {
          console.log(`[EventService] Unsubscribed from room: ${room}`)
          resolve()
        } else {
          console.error(`[EventService] Failed to unsubscribe from room: ${room}`, response)
          reject(new Error(response?.message || "Failed to unsubscribe"))
        }
      })
    })
  }

  /**
   * Send a ping to check connection
   */
  ping(): void {
    if (this.socket) {
      this.socket.emit("ping")
    }
  }

  /**
   * Send agent usage intent event to backend
   * This signals that the user intends to use a specific agent environment
   * and triggers activation if the environment is suspended
   *
   * @param environmentId - Environment UUID
   * @returns Promise resolving to backend response
   */
  async sendAgentUsageIntent(environmentId: string): Promise<any> {
    if (!this.socket) {
      console.warn("[EventService] Cannot send agent_usage_intent: not connected")
      return { status: "error", message: "Not connected" }
    }

    return new Promise((resolve, reject) => {
      this.socket?.emit("agent_usage_intent", { environment_id: environmentId }, (response: any) => {
        if (response?.status === "error") {
          console.error(`[EventService] agent_usage_intent error:`, response)
          reject(new Error(response.message || "Failed to send usage intent"))
        } else {
          console.log(`[EventService] agent_usage_intent sent for environment ${environmentId}, response:`, response)
          resolve(response)
        }
      })
    })
  }

  /**
   * Check if connected
   */
  isConnected(): boolean {
    return this.socket?.connected || false
  }

  /**
   * Get socket ID
   */
  getSocketId(): string | undefined {
    return this.socket?.id
  }

  /**
   * Get current connection status
   */
  getStatus(): ConnectionStatus {
    return this.currentStatus
  }

  /**
   * Subscribe to connection status changes
   * @param listener - Callback function to handle status changes
   * @returns Unsubscribe function
   */
  onStatusChange(listener: ConnectionStatusListener): () => void {
    this.statusListeners.add(listener)
    // Immediately call with current status
    listener(this.currentStatus)

    // Return unsubscribe function
    return () => {
      this.statusListeners.delete(listener)
    }
  }

  /**
   * Update connection status and notify listeners
   */
  private updateStatus(status: ConnectionStatus): void {
    if (this.currentStatus !== status) {
      this.currentStatus = status
      console.log(`[EventService] Status changed to: ${status}`)
      this.statusListeners.forEach((listener) => {
        try {
          listener(status)
        } catch (error) {
          console.error("[EventService] Error in status listener:", error)
        }
      })
    }
  }

  /**
   * Handle incoming event and notify subscribers
   */
  private handleEvent(event: EventData): void {
    // Notify subscribers who are listening to this specific event type
    this.subscriptions.forEach((subscription) => {
      if (subscription.eventType === event.type || subscription.eventType === "*") {
        try {
          subscription.handler(event)
        } catch (error) {
          console.error(`[EventService] Error in event handler for ${event.type}:`, error)
        }
      }
    })
  }

  /**
   * Handle incoming stream event and notify subscribers
   */
  private handleStreamEvent(streamEvent: any): void {
    // Notify subscribers who are listening to "stream_event"
    this.subscriptions.forEach((subscription) => {
      if (subscription.eventType === "stream_event" || subscription.eventType === "*") {
        try {
          subscription.handler(streamEvent)
        } catch (error) {
          console.error(`[EventService] Error in stream event handler:`, error)
        }
      }
    })
  }
}

// Export singleton instance
export const eventService = new EventServiceClass()
