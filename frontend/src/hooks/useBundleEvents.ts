/**
 * useBundleEvents — global WebSocket handler for bundle / install lifecycle.
 *
 * Wires the four Phase 2 events (``bundle_published``,
 * ``install_update_available``, ``install_update_applied``,
 * ``install_update_failed``) to React Query invalidations so any open
 * detail / list view picks up the change without manual refresh.
 *
 * Mounted once at the protected ``_layout`` root — subscriptions are
 * idempotent (the underlying singleton ``eventService`` deduplicates by
 * subscription id), so additional consumers can safely listen with
 * ``useEventSubscription`` for component-local UX (toasts, banners) on top.
 */
import { useQueryClient } from "@tanstack/react-query"

import { invalidateCatalogItem } from "@/components/Catalog/catalogQueries"
import { useMultiEventSubscription, EventTypes } from "@/hooks/useEventBus"

export function useBundleEvents() {
  const queryClient = useQueryClient()

  useMultiEventSubscription(
    [
      EventTypes.BUNDLE_PUBLISHED,
      EventTypes.INSTALL_UPDATE_AVAILABLE,
      EventTypes.INSTALL_UPDATE_APPLIED,
      EventTypes.INSTALL_UPDATE_FAILED,
      EventTypes.CATALOG_UPDATED,
      EventTypes.CATALOG_GRANTED,
    ],
    (event) => {
      switch (event.type) {
        case EventTypes.BUNDLE_PUBLISHED: {
          // Refresh publisher's bundles list + revisions for the affected bundle.
          queryClient.invalidateQueries({ queryKey: ["bundles"] })
          queryClient.invalidateQueries({ queryKey: ["catalog"] })
          if (event.meta?.bundle_uuid) {
            queryClient.invalidateQueries({
              queryKey: ["bundles", event.meta.bundle_uuid],
            })
            queryClient.invalidateQueries({
              queryKey: ["bundles", event.meta.bundle_uuid, "revisions"],
            })
          }
          break
        }
        case EventTypes.INSTALL_UPDATE_AVAILABLE: {
          // Banner/state on the install detail uses ["agent", agentId].
          if (event.model_id) {
            queryClient.invalidateQueries({ queryKey: ["agent", event.model_id] })
          }
          // Agents list cards show the "Update" badge based on pending_update.
          queryClient.invalidateQueries({ queryKey: ["agents"] })
          break
        }
        case EventTypes.INSTALL_UPDATE_APPLIED:
        case EventTypes.INSTALL_UPDATE_FAILED: {
          if (event.model_id) {
            queryClient.invalidateQueries({ queryKey: ["agent", event.model_id] })
            // The env tree caches under ["environments", agentId] in some
            // consumers; broad invalidation is cheap and keeps the active
            // env panel honest.
            queryClient.invalidateQueries({ queryKey: ["environments"] })
          }
          queryClient.invalidateQueries({ queryKey: ["agents"] })
          if (event.type === EventTypes.INSTALL_UPDATE_APPLIED) {
            // The tile's "update available" is derived from the install's
            // revision, which just moved.
            queryClient.invalidateQueries({ queryKey: ["catalog-items"] })
          }
          break
        }
        case EventTypes.CATALOG_UPDATED:
        case EventTypes.CATALOG_GRANTED: {
          // Every catalog list, and the item's own detail when it is open.
          const uuid = event.meta?.uuid
          invalidateCatalogItem(
            queryClient,
            typeof uuid === "string" ? uuid : undefined,
          )
          break
        }
        default:
          break
      }
    },
  )
}
