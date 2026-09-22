import { ServiceUriField } from "@/components/Credentials/CredentialFields/ServiceUriField"
import { UseFormReturn } from "react-hook-form"
import { OdooFields } from "@/components/Credentials/CredentialFields"

interface OdooCredentialFormProps {
  form: UseFormReturn<any>
}

export function OdooCredentialForm({ form }: OdooCredentialFormProps) {
  return <><OdooFields control={form.control} /><ServiceUriField control={form.control} /></>
}
