/**
 * Shared ``provided_by`` → human label mapping for bundle credential
 * provisioning. Used by both the per-row republish hint
 * (CredentialProvisioningSection) and the Revisions-card drift warning
 * (AgentBundleTab) so the two surfaces always agree on wording.
 */
export type ProvidedBy = "user" | "publisher" | "template"

export function providedByLabel(value: ProvidedBy): string {
  switch (value) {
    case "publisher":
      return "embedded (shared)"
    case "template":
      return "template"
    default:
      return "user-provided"
  }
}
