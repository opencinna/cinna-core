import { Control } from "react-hook-form"
import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"

interface OdooFieldsProps {
  control: Control<any>
}

export function OdooFields({ control }: OdooFieldsProps) {
  return (
    <>
      <FormField
        control={control}
        name="credential_data.url"
        render={({ field }) => (
          <FormItem>
            <FormLabel>
              URL <span className="text-destructive">*</span>
            </FormLabel>
            <FormControl>
              <Input placeholder="https://your-odoo.com" {...field} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={control}
        name="credential_data.database_name"
        render={({ field }) => (
          <FormItem>
            <FormLabel>
              Database Name <span className="text-destructive">*</span>
            </FormLabel>
            <FormControl>
              <Input placeholder="production" {...field} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
      <FormField
        control={control}
        name="credential_data.login"
        render={({ field }) => (
          <FormItem>
            <FormLabel>
              Login <span className="text-destructive">*</span>
            </FormLabel>
            <FormControl>
              <Input placeholder="admin" {...field} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
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
