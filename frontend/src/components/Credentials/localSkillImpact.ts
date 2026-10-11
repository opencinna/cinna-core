/**
 * The local-install half of the disable-sharing and delete warnings: people
 * who installed one of the owner's skills on their own computer and receive
 * this credential through it. Counted in people, since the backend counts
 * distinct installers (`active_local_skill_install_count`).
 */
export function localSkillImpactSentence(
  people: number,
  action: "disable" | "delete",
): string {
  const who = people === 1 ? "1 person uses" : `${people} people use`
  const tail =
    action === "disable"
      ? "Turning sharing off leaves those skills without it."
      : "Deleting it leaves those skills without it."
  return `${who} it in your skills on their computer. ${tail}`
}
