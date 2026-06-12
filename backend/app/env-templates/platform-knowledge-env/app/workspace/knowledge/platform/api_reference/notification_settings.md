# Notification Settings — API Reference

Auto-generated from OpenAPI spec. Tag: `notification-settings`

## GET `/api/v1/notification-settings/`
**Read Notification Settings**

**Response:** `NotificationSettingsPublic`

---

## PUT `/api/v1/notification-settings/{notification_type}`
**Update Notification Setting**

**Path parameters:**
- `notification_type`: string

**Request body** (`UserNotificationSettingUpdate`):
  - `email_enabled`: boolean (required)

**Response:** `NotificationSettingItem`

---
