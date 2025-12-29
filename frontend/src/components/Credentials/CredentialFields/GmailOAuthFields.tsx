import { Control } from "react-hook-form"
import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"

interface GmailOAuthFieldsProps {
  control: Control<any>
}

export function GmailOAuthFields({ control }: GmailOAuthFieldsProps) {
  return (
    <>
      <FormField
        control={control}
        name="credential_data.access_token"
        render={({ field }) => (
          <FormItem>
            <FormLabel>
              Access Token <span className="text-destructive">*</span>
            </FormLabel>
            <FormControl>
              <Textarea placeholder="ya29.a0..." {...field} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={control}
        name="credential_data.refresh_token"
        render={({ field }) => (
          <FormItem>
            <FormLabel>Refresh Token</FormLabel>
            <FormControl>
              <Textarea placeholder="1//0g..." {...field} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={control}
        name="credential_data.token_type"
        render={({ field }) => (
          <FormItem>
            <FormLabel>Token Type</FormLabel>
            <FormControl>
              <Input placeholder="Bearer" {...field} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={control}
        name="credential_data.scope"
        render={({ field }) => (
          <FormItem>
            <FormLabel>Scope</FormLabel>
            <FormControl>
              <Input
                placeholder="https://www.googleapis.com/auth/gmail.readonly"
                {...field}
              />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
    </>
  )
}
