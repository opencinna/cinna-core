import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { z } from "zod"
import { Sparkles } from "lucide-react"

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
import { LoadingButton } from "@/components/ui/loading-button"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

const formSchema = z.object({
  handover_prompt: z.string(),
})

type FormData = z.infer<typeof formSchema>

interface EditHandoverPromptModalProps {
  /** The agent that hands work over (the card's own agent). */
  agentId: string
  handoverId: string
  targetAgentId: string
  targetAgentName: string
  currentPrompt: string
  open: boolean
  onClose: () => void
}

/**
 * Edit the prompt one agent passes to another when it hands work over.
 *
 * The only form in the Handover concern: the card itself is a View surface
 * whose controls auto-save, so the prompt — the one thing worth reading in
 * full before it is written — lives here behind explicit Save / Cancel.
 *
 * "Generate with AI" deliberately writes into the field instead of
 * persisting: a generated prompt is a draft the user reads and accepts, and
 * a second auto-saving write path would compete with this form.
 *
 * Mounted only while open, by every caller. That is load-bearing, not an
 * optimisation: `currentPrompt` comes straight from the list query, so a
 * mounted-but-closed instance would re-seed the form from a background
 * refetch and silently discard whatever the user had typed.
 */
export function EditHandoverPromptModal({
  agentId,
  handoverId,
  targetAgentId,
  targetAgentName,
  currentPrompt,
  open,
  onClose,
}: EditHandoverPromptModalProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [generating, setGenerating] = useState(false)

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    defaultValues: { handover_prompt: currentPrompt },
  })

  const mutation = useMutation({
    mutationFn: (data: FormData) =>
      AgentsService.updateHandoverConfig({
        id: agentId,
        handoverId,
        requestBody: { handover_prompt: data.handover_prompt },
      }),
    onSuccess: () => {
      showSuccessToast("Handover prompt saved")
      queryClient.invalidateQueries({ queryKey: ["agentHandovers", agentId] })
      onClose()
    },
    onError: handleError.bind(showErrorToast),
  })

  const handleGenerate = async () => {
    setGenerating(true)
    try {
      const result = await AgentsService.generateHandoverPromptEndpoint({
        id: agentId,
        requestBody: { target_agent_id: targetAgentId },
      })
      if (result.success && result.handover_prompt) {
        form.setValue("handover_prompt", result.handover_prompt, {
          shouldDirty: true,
        })
        showSuccessToast("Handover prompt generated")
      } else {
        showErrorToast(
          result.error ||
            "Couldn't generate a handover prompt. Make sure both agents have a description.",
        )
      }
    } catch (err) {
      handleError.call(showErrorToast, err as ApiError)
    } finally {
      setGenerating(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Handover to {targetAgentName}</DialogTitle>
          <DialogDescription>
            What this agent should tell {targetAgentName} when it hands work
            over.
          </DialogDescription>
        </DialogHeader>

        <Form {...form}>
          <form onSubmit={form.handleSubmit((data) => mutation.mutate(data))}>
            <FormField
              control={form.control}
              name="handover_prompt"
              render={({ field }) => (
                <FormItem>
                  <FormControl>
                    <Textarea
                      placeholder="Describe when to hand over and what context to pass…"
                      className="min-h-[160px]"
                      {...field}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <DialogFooter className="pt-4 mt-4 border-t flex justify-between">
              <Button
                type="button"
                variant="outline"
                onClick={handleGenerate}
                disabled={generating || mutation.isPending}
              >
                <Sparkles className="mr-2 h-4 w-4" />
                {generating ? "Generating…" : "Generate with AI"}
              </Button>
              <div className="flex gap-2 ml-auto">
                <Button type="button" variant="outline" onClick={onClose}>
                  Cancel
                </Button>
                <LoadingButton
                  type="submit"
                  loading={mutation.isPending}
                  disabled={!form.formState.isDirty || generating}
                >
                  Save
                </LoadingButton>
              </div>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
