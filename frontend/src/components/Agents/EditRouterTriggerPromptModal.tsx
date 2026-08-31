import { useEffect, useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { z } from "zod"
import { Wand2 } from "lucide-react"

import type { ApiError } from "@/client"
import { AgentsService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormMessage,
} from "@/components/ui/form"
import { Textarea } from "@/components/ui/textarea"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

const formSchema = z.object({
  router_trigger_prompt: z.string().optional(),
})

type FormData = z.infer<typeof formSchema>

interface EditRouterTriggerPromptModalProps {
  agentId: string
  currentPrompt: string | null | undefined
  open: boolean
  onClose: () => void
  readOnly?: boolean
}

/**
 * Edit / generate the App MCP router Trigger Prompt for an agent.
 *
 * Available to any agent owner (publisher install + foreign installs)
 * via the focused ``PATCH /agents/{id}/router-trigger-prompt`` endpoint.
 * The "Generate" button calls the AI generator built off the agent's
 * description.
 */
export function EditRouterTriggerPromptModal({
  agentId,
  currentPrompt,
  open,
  onClose,
  readOnly = false,
}: EditRouterTriggerPromptModalProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [generating, setGenerating] = useState(false)

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    defaultValues: {
      router_trigger_prompt: currentPrompt ?? "",
    },
  })

  useEffect(() => {
    if (open) {
      form.reset({
        router_trigger_prompt: currentPrompt ?? "",
      })
    }
  }, [open, currentPrompt, form])

  const mutation = useMutation({
    mutationFn: (data: FormData) =>
      AgentsService.updateRouterTriggerPrompt({
        id: agentId,
        requestBody: data,
      }),
    onSuccess: () => {
      showSuccessToast("Trigger prompt updated successfully")
      queryClient.invalidateQueries({ queryKey: ["agents"] })
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] })
      onClose()
    },
    onError: handleError.bind(showErrorToast),
  })

  const handleGenerate = async () => {
    setGenerating(true)
    try {
      const result =
        await AgentsService.generateRouterTriggerPromptEndpoint({
          id: agentId,
        })
      if (result.success && result.trigger_prompt) {
        form.setValue("router_trigger_prompt", result.trigger_prompt, {
          shouldDirty: true,
        })
        showSuccessToast("Trigger prompt generated")
      } else {
        showErrorToast(
          result.error ||
            "Failed to generate trigger prompt. Make sure the agent has a description.",
        )
      }
    } catch (err) {
      handleError.call(showErrorToast, err as ApiError)
    } finally {
      setGenerating(false)
    }
  }

  const onSubmit = (data: FormData) => {
    mutation.mutate(data)
  }

  const handleClose = () => {
    form.reset()
    onClose()
  }

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-3xl max-h-[90vh] flex flex-col">
        <DialogHeader>
          <DialogTitle>
            {readOnly ? "View Trigger Prompt" : "Edit Trigger Prompt"}
          </DialogTitle>
          <DialogDescription>
            A short, capability-focused sentence the App MCP router uses to
            decide when this agent should handle an incoming message
            (e.g., "Plans meetings and books events in my calendar").
          </DialogDescription>
        </DialogHeader>

        <Form {...form}>
          <form
            onSubmit={form.handleSubmit(onSubmit)}
            className="flex flex-col flex-1 min-h-0"
          >
            <div className="flex-1 overflow-auto py-2">
              <FormField
                control={form.control}
                name="router_trigger_prompt"
                render={({ field }) => (
                  <FormItem className="h-full">
                    <FormControl>
                      <Textarea
                        placeholder="When should the router pick this agent? e.g. 'Plans meetings on my calendar...'"
                        className="min-h-[160px] h-full resize-none"
                        {...field}
                        value={field.value || ""}
                        disabled={readOnly}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <DialogFooter className="pt-4 border-t mt-4 flex justify-between">
              {!readOnly && (
                <Button
                  type="button"
                  variant="outline"
                  onClick={handleGenerate}
                  disabled={generating}
                >
                  <Wand2 className="mr-2 h-4 w-4" />
                  {generating ? "Generating..." : "Generate"}
                </Button>
              )}
              <div className="flex gap-2 ml-auto">
                <Button type="button" variant="outline" onClick={handleClose}>
                  {readOnly ? "Close" : "Cancel"}
                </Button>
                {!readOnly && (
                  <LoadingButton
                    type="submit"
                    loading={mutation.isPending}
                    disabled={!form.formState.isDirty}
                  >
                    Save
                  </LoadingButton>
                )}
              </div>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
