import * as CheckboxPrimitive from "@radix-ui/react-checkbox"
import { CheckIcon, MinusIcon } from "lucide-react"
import type * as React from "react"

import { cn } from "@/lib/utils"

/**
 * A checkbox whose indeterminate state reads as "some", not "all": the
 * shadcn `ui/checkbox` renders the same check for both, which makes a
 * select-all header with 2 of 30 rows selected look like every row is.
 * Same box and colours as `ui/checkbox`; a `Minus` when indeterminate.
 */
export function TriStateCheckbox({
  className,
  ...props
}: React.ComponentProps<typeof CheckboxPrimitive.Root>) {
  return (
    <CheckboxPrimitive.Root
      data-slot="checkbox"
      className={cn(
        "peer group/tri border-input dark:bg-input/30 data-[state=checked]:bg-primary data-[state=indeterminate]:bg-primary data-[state=checked]:text-primary-foreground data-[state=indeterminate]:text-primary-foreground dark:data-[state=checked]:bg-primary dark:data-[state=indeterminate]:bg-primary data-[state=checked]:border-primary data-[state=indeterminate]:border-primary focus-visible:border-ring focus-visible:ring-ring/50 size-4 shrink-0 rounded-[4px] border shadow-xs transition-shadow outline-none focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator className="grid place-content-center text-current transition-none">
        <CheckIcon className="size-3.5 group-data-[state=indeterminate]/tri:hidden" />
        <MinusIcon className="hidden size-3.5 group-data-[state=indeterminate]/tri:block" />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
}
