/**
 * The segmented control §2 "Page toolbar" names for section navigation, as
 * class names over the shadcn `Tabs` primitive: one bordered track, the current
 * tab in `--primary`. Never shadcn's raised `bg-background` tile, which on the
 * dark skin is darker than its own track and reads as a hole. The `dark:`
 * overrides are needed because the primitive sets its own `dark:` active
 * background and text colour, which would otherwise win there — the hover
 * colour included, which is why it is restated under `dark:` too.
 *
 * Extracted from `Agents/Addons/SkillDocumentTabs.tsx` at its second consumer
 * (`Agents/ImprovementRequestDetailModal.tsx`): a dialog that splits its facts
 * from a document by tabs.
 */

/** The `TabsList` track. */
export const SEGMENTED_LIST_CLASS =
  "h-auto gap-1 rounded-lg border bg-muted/50 p-1"

/** One `TabsTrigger`. */
export const SEGMENTED_TRIGGER_CLASS =
  "h-auto flex-none rounded-md px-3 py-1.5 text-muted-foreground dark:text-muted-foreground data-[state=inactive]:hover:text-foreground dark:data-[state=inactive]:hover:text-foreground data-[state=inactive]:hover:bg-none data-[state=active]:bg-primary data-[state=active]:text-primary-foreground data-[state=active]:shadow-xs dark:data-[state=active]:border-transparent dark:data-[state=active]:bg-primary dark:data-[state=active]:text-primary-foreground"

/**
 * A `TabsContent` panel is a Tab stop in Radix, and the primitive's
 * `outline-none` would leave it with no visible focus at all.
 */
export const SEGMENTED_PANEL_FOCUS_CLASS =
  "rounded-md focus-visible:ring-[3px] focus-visible:ring-ring/50"

/**
 * A panel inside a dialog body: the body's own spacing and shrink rule,
 * restated one level down — `DialogContent`'s `gap-4` and `[&>*]:min-w-0`
 * reach only its direct children, which is the `Tabs` root.
 */
export const SEGMENTED_DIALOG_PANEL_CLASS = `flex min-w-0 flex-col gap-4 [&>*]:min-w-0 ${SEGMENTED_PANEL_FOCUS_CLASS}`
