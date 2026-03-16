# Files — API Reference

Auto-generated from OpenAPI spec. Tag: `files`

## POST `/api/v1/files/upload`
**Upload File**

**Request body** (`Body_files-upload_file`):
  - `file`: binary (required)

**Response:** `FileUploadPublic`

---

## DELETE `/api/v1/files/{file_id}`
**Delete File**

**Path parameters:**
- `file_id`: uuid

---

## GET `/api/v1/files/{file_id}/download`
**Download File**

**Path parameters:**
- `file_id`: uuid

---
