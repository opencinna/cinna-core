import { AlertCircle } from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import {
  SKILL_FAILURE_NEXT_STEP,
  skillCatalogFailure,
} from "@/utils/skillCatalog"

/**
 * A coded catalog refusal, rendered where the form that caused it can see it.
 *
 * Both mutation dialogs on the catalog (publish, install) fail with the same
 * `{code, message, paths}` body and need the same three things out of it: the
 * server's sentence, the next step for that code, and — for
 * `skill_contains_secrets` — the offending files, one per line. Two copies of
 * that would be two different answers to the same 409 within one feature.
 *
 * Not `QueryErrorAlert`: this is a *write* that was refused, so there is
 * nothing to retry — the user has to change something first, which is what the
 * next-step line is for.
 */
export function SkillCatalogErrorAlert({
  error,
  fallback,
}: {
  error: unknown
  /**
   * What failed, in the caller's words ("Couldn't publish the skill"). It is
   * the title, and it is also the fallback message when the refusal body
   * carried no sentence of its own.
   */
  fallback: string
}) {
  const failure = skillCatalogFailure(error, fallback)
  const nextStep = failure.code
    ? SKILL_FAILURE_NEXT_STEP[failure.code]
    : undefined

  return (
    <Alert variant="destructive">
      <AlertCircle />
      {/* The caller's short phrase in the title and the server's sentence in
          the description, the split `QueryErrorAlert` already uses. `AlertTitle`
          ships `line-clamp-1`, and the server's sentences are full sentences
          ("These files inside the skill look like credentials…") inside an
          `sm:max-w-md` dialog — putting one in the title cut it mid-word. */}
      <AlertTitle>{fallback}</AlertTitle>
      <AlertDescription>
        <p className="text-xs break-words">{failure.message}</p>
        {/* The files come before the advice: "move or delete these files" is
            unactionable until the reader can see which ones. */}
        {failure.paths.length > 0 && (
          <ul className="space-y-0.5">
            {failure.paths.map((path) => (
              <li key={path} className="font-mono text-xs break-all">
                {path}
              </li>
            ))}
          </ul>
        )}
        {nextStep && <p className="text-xs">{nextStep}</p>}
      </AlertDescription>
    </Alert>
  )
}
