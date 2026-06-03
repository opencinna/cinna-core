import { Control } from "react-hook-form"
import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"

interface ServiceUriFieldProps {
  control: Control<any>
}

/**
 * Optional, non-secret `service_uri` (audience/slot id) field.
 *
 * The publisher stamps the same `service_uri` on a bundle's credential spec
 * and on every per-user token for that slot. At install time the matcher uses
 * it as the top-precedence tier so a differently-named per-user token still
 * auto-attaches. It is plaintext, never encrypted, and carries no authority by
 * itself — the token value still gates access.
 */
export function ServiceUriField({ control }: ServiceUriFieldProps) {
  return (
    <FormField
      control={control}
      name="service_uri"
      render={({ field }) => (
        <FormItem>
          <FormLabel>Service URI</FormLabel>
          <FormControl>
            <Input
              placeholder="reddit.com"
              {...field}
              value={field.value ?? ""}
            />
          </FormControl>
          <p className="text-xs text-muted-foreground mt-1">
            A unique identifier for the service this token belongs to — for
            example, <code>reddit.com</code>. Helps identify which service this
            API credential is for. Not secret.
          </p>
          <FormMessage />
        </FormItem>
      )}
    />
  )
}
