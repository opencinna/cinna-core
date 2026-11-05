import useCustomToast from "@/hooks/useCustomToast"
import { skillInstallLink } from "@/utils/skillLinks"

/**
 * **Copy install link** for a skill package, with the usual copied / failed
 * toasts. One copy so the package page's menu and the catalog tile's menu
 * cannot drift into two different links or two different sentences.
 *
 * `navigator.clipboard` is undefined outside a secure context and `writeText`
 * rejects when permission is denied; both land on the failure toast.
 */
export function useCopySkillInstallLink() {
  const { showSuccessToast, showErrorToast } = useCustomToast()
  return async (packageId: string) => {
    try {
      await navigator.clipboard.writeText(skillInstallLink(packageId))
      showSuccessToast("Install link copied")
    } catch {
      showErrorToast("Failed to copy the install link")
    }
  }
}
