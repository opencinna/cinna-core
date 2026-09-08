import { ApiError } from "@/client/core/ApiError"

/**
 * The two 409 shapes `DELETE /admin/ai-providers/{id}` can answer.
 *
 * Neither is on the generated client: the route's `response_model` is
 * `Message`, so the error envelopes are outside the OpenAPI schema and have to
 * be read from `ApiError.body`. They are parsed here, once, with every field
 * checked — the same discipline `parseAutoProvisionConflict` uses and for the
 * same reason: a 409 from a future release carrying the code but not the body
 * must fall through to the generic error toast rather than render a sentence
 * with `undefined` in it.
 *
 * Mirrors `AIProviderDeleteImpact` / `AIProviderDeleteMember` in
 * `backend/app/models/credentials/provider_admin_credential.py` and the two
 * `raise HTTPException(409, ...)` sites in
 * `backend/app/services/credentials/ai_providers_service.py`.
 */
export interface ProviderDeleteMember {
  user_id: string
  email: string
  full_name: string | null
  /** A key minted at the provider exists and will be revoked *there*. */
  holds_provider_key: boolean
}

export interface ProviderDeleteImpact {
  provider_id: string
  provider_name: string
  member_count: number
  /** How many of those keys are destroyed at the vendor. 0 for a fixed key. */
  minted_key_count: number
  members: ProviderDeleteMember[]
}

function asRecord(error: unknown): Record<string, unknown> | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const detail = (error.body as { detail?: unknown } | undefined)?.detail
  if (!detail || typeof detail !== "object") return null
  return detail as Record<string, unknown>
}

/** The impact body of a refused unforced delete, or `null` for anything else. */
export function parseProviderDeleteImpact(
  error: unknown,
): ProviderDeleteImpact | null {
  const detail = asRecord(error)
  if (!detail || detail.code !== "ai_provider_in_use") return null
  const impact = detail.impact
  if (!impact || typeof impact !== "object") return null
  const body = impact as Record<string, unknown>
  if (
    typeof body.provider_id !== "string" ||
    typeof body.provider_name !== "string" ||
    typeof body.member_count !== "number" ||
    typeof body.minted_key_count !== "number" ||
    !Array.isArray(body.members)
  ) {
    return null
  }
  const members: ProviderDeleteMember[] = []
  for (const entry of body.members) {
    if (!entry || typeof entry !== "object") continue
    const member = entry as Record<string, unknown>
    if (typeof member.user_id !== "string" || typeof member.email !== "string") {
      continue
    }
    members.push({
      user_id: member.user_id,
      email: member.email,
      full_name:
        typeof member.full_name === "string" ? member.full_name : null,
      holds_provider_key: member.holds_provider_key === true,
    })
  }
  return {
    provider_id: body.provider_id,
    provider_name: body.provider_name,
    member_count: body.member_count,
    minted_key_count: body.minted_key_count,
    members,
  }
}

/**
 * The *second* 409: the forced delete removed what it could, some members
 * could not be removed, and the provider was left standing.
 *
 * Returns the server's own sentence, which already names the count and says
 * to try again. Retrying is what completes the removal, so the caller offers
 * that rather than closing the dialog on a dead end.
 */
export function parseMembersNotRemoved(error: unknown): string | null {
  const detail = asRecord(error)
  if (!detail || detail.code !== "ai_provider_members_not_removed") return null
  return typeof detail.message === "string"
    ? detail.message
    : "Some members could not be removed, so the provider is still here."
}
