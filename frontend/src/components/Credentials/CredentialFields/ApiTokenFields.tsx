import { Control, UseFormWatch } from "react-hook-form"
import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

interface ApiTokenFieldsProps {
  control: Control<any>
  watch: UseFormWatch<any>
}

export function ApiTokenFields({ control, watch }: ApiTokenFieldsProps) {
  const tokenType = watch("credential_data.api_token_type")

  return (
    <>
      <FormField
        control={control}
        name="credential_data.api_token_type"
        render={({ field }) => (
          <FormItem>
            <FormLabel>
              API Token Type <span className="text-destructive">*</span>
            </FormLabel>
            <Select
              onValueChange={field.onChange}
              value={field.value as string || "bearer"}
            >
              <FormControl>
                <SelectTrigger>
                  <SelectValue placeholder="Select token type" />
                </SelectTrigger>
              </FormControl>
              <SelectContent>
                <SelectItem value="bearer">Bearer</SelectItem>
                <SelectItem value="custom">Custom</SelectItem>
              </SelectContent>
            </Select>
            <FormMessage />
          </FormItem>
        )}
      />
      {tokenType === "custom" && (
        <FormField
          control={control}
          name="credential_data.api_token_template"
          render={({ field }) => (
            <FormItem>
              <FormLabel>
                API Token Template <span className="text-destructive">*</span>
              </FormLabel>
              <FormControl>
                <Input
                  placeholder="Authorization: Bearer {TOKEN}"
                  {...field}
                />
              </FormControl>
              <p className="text-xs text-muted-foreground mt-1">
                Use {"{TOKEN}"} as placeholder. Example: "Authorization: Bearer {"{TOKEN}"}" or "X-API-Key: {"{TOKEN}"}"
              </p>
              <FormMessage />
            </FormItem>
          )}
        />
      )}
      <FormField
        control={control}
        name="credential_data.api_token"
        render={({ field }) => (
          <FormItem>
            <FormLabel>
              API Token <span className="text-destructive">*</span>
            </FormLabel>
            <FormControl>
              <Input type="password" placeholder="••••••••" {...field} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
    </>
  )
}
