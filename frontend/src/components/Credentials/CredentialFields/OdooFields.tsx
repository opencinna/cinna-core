import { useState } from "react"
import { Control, useWatch } from "react-hook-form"
import { useMutation } from "@tanstack/react-query"
import { CheckCircle2, XCircle, Loader2 } from "lucide-react"
import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Button } from "@/components/ui/button"
import { CredentialsService } from "@/client"

interface OdooFieldsProps {
  control: Control<any>
}

export function OdooFields({ control }: OdooFieldsProps) {
  const [verificationResult, setVerificationResult] = useState<{
    success: boolean
    message: string
  } | null>(null)

  // Watch the credential_data fields to get current values
  const credentialData = useWatch({
    control,
    name: "credential_data",
  })

  const verifyMutation = useMutation({
    mutationFn: () =>
      CredentialsService.verifyOdooCredential({
        requestBody: {
          url: credentialData?.url || "",
          database_name: credentialData?.database_name || "",
          login: credentialData?.login || "",
          api_token: credentialData?.api_token || "",
        },
      }),
    onSuccess: (data) => {
      setVerificationResult({
        success: data.success,
        message: data.message,
      })
    },
    onError: (error: any) => {
      setVerificationResult({
        success: false,
        message: error.message || "Verification failed",
      })
    },
  })

  const canVerify =
    credentialData?.url &&
    credentialData?.database_name &&
    credentialData?.login &&
    credentialData?.api_token

  const handleVerify = () => {
    setVerificationResult(null)
    verifyMutation.mutate()
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
      {/* Left Column: Name and Notes */}
      <div className="space-y-4">
        <FormField
          control={control}
          name="name"
          render={({ field }) => (
            <FormItem>
              <FormLabel>
                Name <span className="text-destructive">*</span>
              </FormLabel>
              <FormControl>
                <Input placeholder="My Odoo Credential" type="text" {...field} />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={control}
          name="notes"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Notes</FormLabel>
              <FormControl>
                <Textarea
                  placeholder="Additional notes..."
                  className="min-h-[200px]"
                  {...field}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
      </div>

      {/* Right Column: Odoo Connection Details */}
      <div className="space-y-4">
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

        {/* Verify Button and Result */}
        <div className="pt-2">
          <Button
            type="button"
            variant="outline"
            onClick={handleVerify}
            disabled={!canVerify || verifyMutation.isPending}
          >
            {verifyMutation.isPending && (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            )}
            Verify Connection
          </Button>

          {verificationResult && (
            <div
              className={`mt-3 flex items-start gap-2 text-sm ${
                verificationResult.success
                  ? "text-green-600"
                  : "text-destructive"
              }`}
            >
              {verificationResult.success ? (
                <CheckCircle2 className="h-4 w-4 mt-0.5 shrink-0" />
              ) : (
                <XCircle className="h-4 w-4 mt-0.5 shrink-0" />
              )}
              <span>{verificationResult.message}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
