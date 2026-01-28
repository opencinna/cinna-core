/**
 * Manual API service for task triggers.
 *
 * Replace with auto-generated client after running `make gen-client`.
 */
import { OpenAPI } from "@/client"
import { request as __request } from "@/client/core/request"

// ==================== Types ====================

export type TriggerType = "schedule" | "exact_date" | "webhook"

export interface TaskTriggerPublic {
  id: string
  task_id: string
  type: TriggerType
  name: string
  enabled: boolean
  payload_template: string | null
  // Schedule fields
  cron_string: string | null
  timezone: string | null
  schedule_description: string | null
  last_execution: string | null
  next_execution: string | null
  // Exact date fields
  execute_at: string | null
  executed: boolean
  // Webhook fields
  webhook_id: string | null
  webhook_token_prefix: string | null
  webhook_url: string | null
  // Timestamps
  created_at: string
  updated_at: string
}

export interface TaskTriggerPublicWithToken extends TaskTriggerPublic {
  webhook_token: string | null
}

export interface TaskTriggersPublic {
  data: TaskTriggerPublic[]
  count: number
}

export interface TaskTriggerCreateSchedule {
  name: string
  type: "schedule"
  payload_template?: string | null
  natural_language: string
  timezone: string
}

export interface TaskTriggerCreateExactDate {
  name: string
  type: "exact_date"
  payload_template?: string | null
  execute_at: string
  timezone: string
}

export interface TaskTriggerCreateWebhook {
  name: string
  type: "webhook"
  payload_template?: string | null
}

export interface TaskTriggerUpdate {
  name?: string | null
  enabled?: boolean | null
  payload_template?: string | null
  natural_language?: string | null
  timezone?: string | null
  execute_at?: string | null
}

// ==================== API Service ====================

export const TaskTriggersApi = {
  listTriggers(taskId: string): Promise<TaskTriggersPublic> {
    return __request(OpenAPI, {
      method: "GET",
      url: "/api/v1/tasks/{task_id}/triggers",
      path: { task_id: taskId },
    })
  },

  createScheduleTrigger(
    taskId: string,
    data: TaskTriggerCreateSchedule,
  ): Promise<TaskTriggerPublic> {
    return __request(OpenAPI, {
      method: "POST",
      url: "/api/v1/tasks/{task_id}/triggers/schedule",
      path: { task_id: taskId },
      body: data,
      mediaType: "application/json",
    })
  },

  createExactDateTrigger(
    taskId: string,
    data: TaskTriggerCreateExactDate,
  ): Promise<TaskTriggerPublic> {
    return __request(OpenAPI, {
      method: "POST",
      url: "/api/v1/tasks/{task_id}/triggers/exact-date",
      path: { task_id: taskId },
      body: data,
      mediaType: "application/json",
    })
  },

  createWebhookTrigger(
    taskId: string,
    data: TaskTriggerCreateWebhook,
  ): Promise<TaskTriggerPublicWithToken> {
    return __request(OpenAPI, {
      method: "POST",
      url: "/api/v1/tasks/{task_id}/triggers/webhook",
      path: { task_id: taskId },
      body: data,
      mediaType: "application/json",
    })
  },

  updateTrigger(
    taskId: string,
    triggerId: string,
    data: TaskTriggerUpdate,
  ): Promise<TaskTriggerPublic> {
    return __request(OpenAPI, {
      method: "PATCH",
      url: "/api/v1/tasks/{task_id}/triggers/{trigger_id}",
      path: { task_id: taskId, trigger_id: triggerId },
      body: data,
      mediaType: "application/json",
    })
  },

  deleteTrigger(taskId: string, triggerId: string): Promise<{ success: boolean }> {
    return __request(OpenAPI, {
      method: "DELETE",
      url: "/api/v1/tasks/{task_id}/triggers/{trigger_id}",
      path: { task_id: taskId, trigger_id: triggerId },
    })
  },

  regenerateToken(
    taskId: string,
    triggerId: string,
  ): Promise<TaskTriggerPublicWithToken> {
    return __request(OpenAPI, {
      method: "POST",
      url: "/api/v1/tasks/{task_id}/triggers/{trigger_id}/regenerate-token",
      path: { task_id: taskId, trigger_id: triggerId },
    })
  },
}
