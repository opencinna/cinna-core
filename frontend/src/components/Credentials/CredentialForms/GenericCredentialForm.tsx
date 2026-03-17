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
import { EmailImapFields, EmailSmtpFields } from "@/components/Credentials/CredentialFields"

interface GenericCredentialFormProps {
  form: UseFormReturn<any>
  credentialType: string
}

export function GenericCredentialForm({
  form,
  credentialType,
}: GenericCredentialFormProps) {
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

      {credentialType === "email_imap" && (
        <EmailImapFields control={form.control} />
      )}

      {credentialType === "email_smtp" && (
        <EmailSmtpFields control={form.control} />
      )}

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
