import { zodResolver } from "@hookform/resolvers/zod"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { LoadingButton } from "@/components/ui/loading-button"

const recoverySchema = z.object({
  code: z
    .string()
    .min(8, { message: "Recovery codes are at least 8 characters" })
    .max(32, { message: "Recovery code looks too long" })
    .transform((value) => value.toUpperCase().replace(/[\s-]/gu, "")),
})

export type RecoveryCodeFormData = z.infer<typeof recoverySchema>

interface RecoveryCodeFormProps {
  onSubmit: (data: RecoveryCodeFormData) => void
  loading?: boolean
  buttonLabel?: string
}

/**
 * Single-field recovery code form. Normalizes the value (uppercase,
 * dashes/whitespace stripped) before handing off to the parent.
 */
export function RecoveryCodeForm({
  onSubmit,
  loading = false,
  buttonLabel = "Use recovery code",
}: RecoveryCodeFormProps) {
  const form = useForm<RecoveryCodeFormData>({
    resolver: zodResolver(recoverySchema),
    mode: "onSubmit",
    defaultValues: { code: "" },
  })

  return (
    <Form {...form}>
      <form
        onSubmit={form.handleSubmit(onSubmit)}
        className="flex flex-col gap-4"
      >
        <FormField
          control={form.control}
          name="code"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Recovery code</FormLabel>
              <FormControl>
                <Input
                  {...field}
                  type="text"
                  placeholder="xxxx-xxxx-xx"
                  autoComplete="off"
                  spellCheck={false}
                  aria-label="Recovery code"
                />
              </FormControl>
              <FormMessage className="text-xs" />
            </FormItem>
          )}
        />
        <LoadingButton type="submit" loading={loading}>
          {buttonLabel}
        </LoadingButton>
      </form>
    </Form>
  )
}
