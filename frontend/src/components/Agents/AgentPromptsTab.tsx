import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useEffect } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import type { AgentPublic } from "@/client"
import { AgentsService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Textarea } from "@/components/ui/textarea"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

const formSchema = z.object({
  workflow_prompt: z.string().optional(),
  entrypoint_prompt: z.string().optional(),
})

type FormData = z.infer<typeof formSchema>

interface AgentPromptsTabProps {
  agent: AgentPublic
}

export function AgentPromptsTab({ agent }: AgentPromptsTabProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      workflow_prompt: "",
      entrypoint_prompt: "",
    },
  })

  useEffect(() => {
    if (agent) {
      form.reset({
        workflow_prompt: agent.workflow_prompt ?? undefined,
        entrypoint_prompt: agent.entrypoint_prompt ?? undefined,
      })
    }
  }, [agent, form])

  const mutation = useMutation({
    mutationFn: (data: FormData) =>
      AgentsService.updateAgent({ id: agent.id, requestBody: data }),
    onSuccess: () => {
      showSuccessToast("Prompts updated successfully")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] })
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
    },
  })

  const onSubmit = (data: FormData) => {
    mutation.mutate(data)
  }

  const handleReset = () => {
    form.reset({
      workflow_prompt: agent.workflow_prompt ?? undefined,
      entrypoint_prompt: agent.entrypoint_prompt ?? undefined,
    })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Agent Prompts</CardTitle>
        <CardDescription>
          Configure the workflow and entrypoint prompts for this agent.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
            <FormField
              control={form.control}
              name="workflow_prompt"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Workflow Prompt</FormLabel>
                  <FormControl>
                    <Textarea
                      placeholder="Enter workflow prompt..."
                      className="min-h-[150px]"
                      {...field}
                      value={field.value || ""}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="entrypoint_prompt"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Entrypoint Prompt</FormLabel>
                  <FormControl>
                    <Textarea
                      placeholder="Enter entrypoint prompt..."
                      className="min-h-[150px]"
                      {...field}
                      value={field.value || ""}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={handleReset}
                disabled={mutation.isPending}
              >
                Reset
              </Button>
              <LoadingButton type="submit" loading={mutation.isPending}>
                Save Prompts
              </LoadingButton>
            </div>
          </form>
        </Form>
      </CardContent>
    </Card>
  )
}
