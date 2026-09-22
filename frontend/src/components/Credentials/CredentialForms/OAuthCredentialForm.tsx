import { ServiceUriField } from "@/components/Credentials/CredentialFields/ServiceUriField"
import { UseFormReturn } from "react-hook-form"
import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { OAuthCredentialFields } from "@/components/Credentials/CredentialFields"

interface OAuthCredentialFormProps {
  form: UseFormReturn<any>
  credentialType: string
  credentialId: string
}

export function OAuthCredentialForm({
  form,
  credentialType,
  credentialId,
}: OAuthCredentialFormProps) {
  return (
    <>
      <FormField
        control={form.control}
        name="name"
        render={({ field }) => (
          <FormItem>
            <FormLabel>
              Name <span className="text-destructive">*</span>
            </FormLabel>
            <FormControl>
              <Input placeholder="My Credential" type="text" {...field} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />

      <OAuthCredentialFields
        credentialType={credentialType}
        credentialId={credentialId}
      />

      <ServiceUriField control={form.control} />

      <FormField
        control={form.control}
        name="notes"
        render={({ field }) => (
          <FormItem>
            <FormLabel>Notes</FormLabel>
            <FormControl>
              <Textarea placeholder="Additional notes..." {...field} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
    </>
  )
}
