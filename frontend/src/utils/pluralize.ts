/** "1 key" / "3 keys" — a count with its unit (§2 Fact labels). */
export function pluralize(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`
}
