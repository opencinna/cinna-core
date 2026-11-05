import { createFileRoute } from "@tanstack/react-router"

import { SkillLinkPage } from "@/components/SkillLink/SkillLinkPage"
import { APP_NAME } from "@/utils"

/**
 * Public skill link — deliberately no `beforeLoad`/`ensureSessionValid`, like
 * `/desktop`. Whoever was sent the link may not be signed in to this instance,
 * and the page's job (hand the skill to Cinna Desktop) needs no account here.
 * `$packageId` is the reverse-DNS `package_id`, not the package uuid.
 */
export const Route = createFileRoute("/get/skill/$packageId")({
  component: SkillLinkRoute,
  head: () => ({
    meta: [{ title: `Shared skill - ${APP_NAME}` }],
  }),
})

function SkillLinkRoute() {
  const { packageId } = Route.useParams()
  return <SkillLinkPage packageId={packageId} />
}
