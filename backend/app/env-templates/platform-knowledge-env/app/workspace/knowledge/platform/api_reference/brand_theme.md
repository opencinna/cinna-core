# Brand Theme — API Reference

Auto-generated from OpenAPI spec. Tag: `brand-theme`

## GET `/api/v1/brand-theme`
**Get Brand Theme**

**Response:** `BrandThemePublic`

---

## GET `/api/v1/brand-theme/assets/{sha256}`
**Get Brand Theme Asset**

**Path parameters:**
- `sha256`: string

---

## GET `/api/v1/admin/brand-theme/drafts`
**List Brand Theme Drafts**

> Not available via `cinna api` (excluded_path).

**Response:** `BrandThemeDraftsPublic`

---

## POST `/api/v1/admin/brand-theme/drafts`
**Upload Brand Theme Draft**

> Not available via `cinna api` (excluded_path).

**Request body** (`Body_brand-theme-upload_brand_theme_draft`):
  - `package`: string | null — The package as a .zip
  - `theme`: string | null — theme.json, when not sending a zip
  - `files`: array | null — Asset files, keyed by file name

**Response:** `BrandThemeUploadResult`

---

## GET `/api/v1/admin/brand-theme/drafts/{theme_id}`
**Get Brand Theme Draft**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `theme_id`: uuid

**Response:** `BrandThemeDraftPublic`

---

## DELETE `/api/v1/admin/brand-theme/drafts/{theme_id}`
**Delete Brand Theme Draft**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `theme_id`: uuid

---

## POST `/api/v1/admin/brand-theme/drafts/{theme_id}/publish`
**Publish Brand Theme Draft**

> Not available via `cinna api` (excluded_path).

**Path parameters:**
- `theme_id`: uuid

**Response:** `BrandThemeDraftPublic`

---

## POST `/api/v1/admin/brand-theme/unpublish`
**Unpublish Brand Theme**

> Not available via `cinna api` (excluded_path).

---
