/** Size and count wording shared by the upload picker and artifacts. */

const UNITS = ["B", "KB", "MB", "GB"] as const

/** "4.2 MB" — one decimal under 10 of a unit, none above. */
export function formatBytes(bytes: number | null | undefined): string {
  let value = Math.max(bytes ?? 0, 0)
  let unit = 0
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  const digits = unit === 0 || value >= 10 ? 0 : 1
  return `${value.toFixed(digits)} ${UNITS[unit]}`
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`
}
