import { ServiceUriField } from "@/components/Credentials/CredentialFields/ServiceUriField"
import { UseFormReturn } from "react-hook-form"
import { ServiceAccountFields } from "@/components/Credentials/CredentialFields"

interface ServiceAccountCredentialFormProps {
  form: UseFormReturn<any>
}

export function ServiceAccountCredentialForm({ form }: ServiceAccountCredentialFormProps) {
  return <><ServiceAccountFields control={form.control} /><ServiceUriField control={form.control} /></>
}
