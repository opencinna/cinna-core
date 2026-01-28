import { useState, useCallback } from "react"
import { Control } from "react-hook-form"
import { AlertTriangle, Upload, CheckCircle2 } from "lucide-react"
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
import {
  Alert,
  AlertDescription,
} from "@/components/ui/alert"

interface ServiceAccountFieldsProps {
  control: Control<any>
}

const SA_REQUIRED_FIELDS = ["type", "project_id", "private_key_id", "private_key", "client_email"]

function validateServiceAccountJson(jsonStr: string): { valid: boolean; error?: string; data?: Record<string, any> } {
  if (!jsonStr.trim()) {
    return { valid: false, error: "Service account JSON is required" }
  }

  let parsed: Record<string, any>
  try {
    parsed = JSON.parse(jsonStr)
  } catch {
    return { valid: false, error: "Invalid JSON format" }
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { valid: false, error: "JSON must be an object" }
  }

  if (parsed.type !== "service_account") {
    return { valid: false, error: `Field 'type' must be 'service_account', got '${parsed.type || "(missing)"}'` }
  }

  const missing = SA_REQUIRED_FIELDS.filter(f => !parsed[f])
  if (missing.length > 0) {
    return { valid: false, error: `Missing required fields: ${missing.join(", ")}` }
  }

  return { valid: true, data: parsed }
}

export function ServiceAccountFields({ control }: ServiceAccountFieldsProps) {
  const [validationResult, setValidationResult] = useState<{ valid: boolean; error?: string; data?: Record<string, any> } | null>(null)

  const handleFileUpload = useCallback((
    event: React.ChangeEvent<HTMLInputElement>,
    onChange: (value: any) => void,
    setJsonText: (value: string) => void,
  ) => {
    const file = event.target.files?.[0]
    if (!file) return

    const reader = new FileReader()
    reader.onload = (e) => {
      const text = e.target?.result as string
      if (text) {
        // Validate the JSON
        const result = validateServiceAccountJson(text)
        setValidationResult(result)

        if (result.valid && result.data) {
          // Set the credential_data to the parsed JSON
          onChange(result.data)
          setJsonText(text)
        } else {
          // Still show the text so user can see what's wrong
          setJsonText(text)
        }
      }
    }
    reader.readAsText(file)

    // Reset input so the same file can be re-selected
    event.target.value = ""
  }, [])

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
                <Input placeholder="My Service Account" type="text" {...field} />
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

      {/* Right Column: Service Account JSON */}
      <div className="space-y-4">
        <Alert variant="default" className="border-amber-200 bg-amber-50 text-amber-900">
          <AlertTriangle className="h-4 w-4 text-amber-600" />
          <AlertDescription className="text-xs">
            The JSON key file contains a private key. It will be encrypted and stored securely.
          </AlertDescription>
        </Alert>

        <FormField
          control={control}
          name="credential_data"
          render={({ field }) => {
            // Use a local state for the textarea text representation
            const [jsonText, setJsonText] = useState(() => {
              if (field.value && typeof field.value === "object" && Object.keys(field.value).length > 0) {
                return JSON.stringify(field.value, null, 2)
              }
              return ""
            })

            return (
              <FormItem>
                <div className="flex items-center justify-between">
                  <FormLabel>
                    Service Account JSON <span className="text-destructive">*</span>
                  </FormLabel>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-7 text-xs"
                    onClick={() => {
                      const input = document.createElement("input")
                      input.type = "file"
                      input.accept = ".json"
                      input.onchange = (e) => handleFileUpload(
                        e as any,
                        field.onChange,
                        setJsonText,
                      )
                      input.click()
                    }}
                  >
                    <Upload className="h-3 w-3 mr-1" />
                    Upload JSON
                  </Button>
                </div>
                <FormControl>
                  <Textarea
                    placeholder='Paste your Google Service Account JSON key file contents here...'
                    className="min-h-[280px] font-mono text-xs"
                    value={jsonText}
                    onChange={(e) => {
                      const text = e.target.value
                      setJsonText(text)

                      // Try to parse and set credential_data
                      if (text.trim()) {
                        const result = validateServiceAccountJson(text)
                        setValidationResult(result)
                        if (result.valid && result.data) {
                          field.onChange(result.data)
                        }
                      } else {
                        setValidationResult(null)
                        field.onChange({})
                      }
                    }}
                    onBlur={() => {
                      if (jsonText.trim()) {
                        const result = validateServiceAccountJson(jsonText)
                        setValidationResult(result)
                      }
                    }}
                  />
                </FormControl>
                <FormMessage />

                {/* Validation feedback */}
                {validationResult && !validationResult.valid && (
                  <p className="text-xs text-destructive mt-1">
                    {validationResult.error}
                  </p>
                )}
                {validationResult?.valid && validationResult.data && (
                  <div className="flex items-start gap-2 mt-2 p-2 rounded-md bg-green-50 border border-green-200">
                    <CheckCircle2 className="h-4 w-4 text-green-600 mt-0.5 shrink-0" />
                    <div className="text-xs text-green-800">
                      <p className="font-medium">Valid service account key</p>
                      <p className="text-green-700 mt-0.5">
                        Project: {validationResult.data.project_id}
                        <br />
                        Email: {validationResult.data.client_email}
                      </p>
                    </div>
                  </div>
                )}
              </FormItem>
            )
          }}
        />
      </div>
    </div>
  )
}
