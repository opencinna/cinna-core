import { Control } from "react-hook-form"
import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { Checkbox } from "@/components/ui/checkbox"

interface EmailSmtpFieldsProps {
  control: Control<any>
}

export function EmailSmtpFields({ control }: EmailSmtpFieldsProps) {
  return (
    <>
      <FormField
        control={control}
        name="credential_data.host"
        render={({ field }) => (
          <FormItem>
            <FormLabel>
              Host <span className="text-destructive">*</span>
            </FormLabel>
            <FormControl>
              <Input placeholder="smtp.gmail.com" {...field} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={control}
        name="credential_data.port"
        render={({ field }) => (
          <FormItem>
            <FormLabel>
              Port <span className="text-destructive">*</span>
            </FormLabel>
            <FormControl>
              <Input type="number" placeholder="587" {...field} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={control}
        name="credential_data.username"
        render={({ field }) => (
          <FormItem>
            <FormLabel>
              Username <span className="text-destructive">*</span>
            </FormLabel>
            <FormControl>
              <Input placeholder="user@example.com" {...field} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={control}
        name="credential_data.password"
        render={({ field }) => (
          <FormItem>
            <FormLabel>
              Password <span className="text-destructive">*</span>
            </FormLabel>
            <FormControl>
              <Input type="password" placeholder="••••••••" {...field} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={control}
        name="credential_data.from_email"
        render={({ field }) => (
          <FormItem>
            <FormLabel>
              From Email <span className="text-destructive">*</span>
            </FormLabel>
            <FormControl>
              <Input placeholder="sender@example.com" {...field} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={control}
        name="credential_data.use_tls"
        render={({ field }) => (
          <FormItem className="flex flex-row items-start space-x-3 space-y-0">
            <FormControl>
              <Checkbox
                checked={field.value as boolean}
                onCheckedChange={field.onChange}
              />
            </FormControl>
            <div className="space-y-1 leading-none">
              <FormLabel>Use TLS (STARTTLS)</FormLabel>
            </div>
          </FormItem>
        )}
      />
      <FormField
        control={control}
        name="credential_data.use_ssl"
        render={({ field }) => (
          <FormItem className="flex flex-row items-start space-x-3 space-y-0">
            <FormControl>
              <Checkbox
                checked={field.value as boolean}
                onCheckedChange={field.onChange}
              />
            </FormControl>
            <div className="space-y-1 leading-none">
              <FormLabel>Use SSL</FormLabel>
            </div>
          </FormItem>
        )}
      />
    </>
  )
}
