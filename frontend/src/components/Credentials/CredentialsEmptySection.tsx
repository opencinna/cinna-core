import type { ReactNode } from "react"

/** A `/credentials` section's empty state: one sentence and its action. */
export function CredentialsEmptySection({
  text,
  children,
}: {
  text: string
  children?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center gap-3 py-6 whitespace-normal">
      <p className="max-w-md text-sm text-muted-foreground">{text}</p>
      {children}
    </div>
  )
}
