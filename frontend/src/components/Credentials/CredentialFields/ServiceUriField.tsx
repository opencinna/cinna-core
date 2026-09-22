import { CircleHelp } from "lucide-react"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
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
          <div className="flex items-center gap-1.5">
            <FormLabel>Service URI</FormLabel>
            <Tooltip>
              <TooltipTrigger asChild>
                <button type="button" aria-label="About Service URI" className="text-muted-foreground hover:text-foreground">
                  <CircleHelp className="h-4 w-4" />
                </button>
              </TooltipTrigger>
              <TooltipContent className="max-w-xs">
                A non-secret identifier for the service, such as slack.com or work-mail.
                Agents use it together with the credential type to match a required slot.
                Use the same Service URI on the credential and the agent’s requirement.
              </TooltipContent>
            </Tooltip>
          </div>
          <FormControl>
            <Input
              placeholder="e.g. slack.com or work-mail"
              {...field}
              value={field.value ?? ""}
            />
          </FormControl>
          <FormMessage />
        </FormItem>
      )}
    />
  )
}
