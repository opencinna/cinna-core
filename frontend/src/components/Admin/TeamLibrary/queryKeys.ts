/** React Query keys of the team-library admin surfaces, in one place. */
export const libraryQueryKeys = {
  admin: ["library", "admin"] as const,
  grants: (repoId: string) => ["library", "grants", repoId] as const,
}
