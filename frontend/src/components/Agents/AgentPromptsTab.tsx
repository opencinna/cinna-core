import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
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
  FormMessage,
} from "@/components/ui/form"
import { Textarea } from "@/components/ui/textarea"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import { SmartScheduler } from "./SmartScheduler"

const entrypointFormSchema = z.object({
  entrypoint_prompt: z.string().optional(),
})

const workflowFormSchema = z.object({
  workflow_prompt: z.string().optional(),
})

type EntrypointFormData = z.infer<typeof entrypointFormSchema>
type WorkflowFormData = z.infer<typeof workflowFormSchema>

interface AgentPromptsTabProps {
  agent: AgentPublic
}

export function AgentPromptsTab({ agent }: AgentPromptsTabProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // Entrypoint form
  const entrypointForm = useForm<EntrypointFormData>({
    resolver: zodResolver(entrypointFormSchema),
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      entrypoint_prompt: "",
    },
  })

  // Workflow form
  const workflowForm = useForm<WorkflowFormData>({
    resolver: zodResolver(workflowFormSchema),
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      workflow_prompt: "",
    },
  })

  // Fetch current schedule
  const { data: schedule, refetch: refetchSchedule } = useQuery({
    queryKey: ["agentSchedule", agent.id],
    queryFn: () => AgentsService.getSchedule({ id: agent.id }),
    enabled: !!agent.id,
  })

  useEffect(() => {
    if (agent) {
      entrypointForm.reset({
        entrypoint_prompt: agent.entrypoint_prompt ?? undefined,
      })
      workflowForm.reset({
        workflow_prompt: agent.workflow_prompt ?? undefined,
      })
    }
  }, [agent, entrypointForm, workflowForm])

  const entrypointMutation = useMutation({
    mutationFn: (data: EntrypointFormData) =>
      AgentsService.updateAgent({ id: agent.id, requestBody: data }),
    onSuccess: () => {
      showSuccessToast("Entrypoint prompt updated successfully")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] })
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
    },
  })

  const workflowMutation = useMutation({
    mutationFn: (data: WorkflowFormData) =>
      AgentsService.updateAgent({ id: agent.id, requestBody: data }),
    onSuccess: () => {
      showSuccessToast("Workflow prompt updated successfully")
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] })
      queryClient.invalidateQueries({ queryKey: ["agent", agent.id] })
    },
  })

  const onEntrypointSubmit = (data: EntrypointFormData) => {
    entrypointMutation.mutate(data)
  }

  const onWorkflowSubmit = (data: WorkflowFormData) => {
    workflowMutation.mutate(data)
  }

  const handleEntrypointReset = () => {
    entrypointForm.reset({
      entrypoint_prompt: agent.entrypoint_prompt ?? undefined,
    })
  }

  const handleWorkflowReset = () => {
    workflowForm.reset({
      workflow_prompt: agent.workflow_prompt ?? undefined,
    })
  }

  return (
    <div className="space-y-6">
      {/* Top Row: Entrypoint and Scheduler (side by side) */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Entrypoint Prompt Card */}
        <Card>
          <CardHeader>
            <CardTitle>Entrypoint Prompt</CardTitle>
            <CardDescription>
              Simple, natural user question that triggers the agent (e.g.,
              "Summarize my unread emails.")
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Form {...entrypointForm}>
              <form
                onSubmit={entrypointForm.handleSubmit(onEntrypointSubmit)}
                className="space-y-4"
              >
                <FormField
                  control={entrypointForm.control}
                  name="entrypoint_prompt"
                  render={({ field }) => (
                    <FormItem>
                      <FormControl>
                        <Textarea
                          placeholder="Enter entrypoint prompt..."
                          className="min-h-[80px]"
                          {...field}
                          value={field.value || ""}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />

                {entrypointForm.formState.isDirty && (
                  <div className="flex justify-end gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      onClick={handleEntrypointReset}
                      disabled={entrypointMutation.isPending}
                    >
                      Reset
                    </Button>
                    <LoadingButton
                      type="submit"
                      loading={entrypointMutation.isPending}
                    >
                      Apply Prompt
                    </LoadingButton>
                  </div>
                )}
              </form>
            </Form>
          </CardContent>
        </Card>

        {/* Scheduler Card */}
        <Card>
          <CardHeader>
            <CardTitle>Scheduler</CardTitle>
            <CardDescription>
              Schedule execution time for this agent with entrypoint prompt as
              starting message
            </CardDescription>
          </CardHeader>
          <CardContent>
            <SmartScheduler
              agentId={agent.id}
              currentSchedule={schedule ?? undefined}
              onScheduleUpdate={() => refetchSchedule()}
            />
          </CardContent>
        </Card>
      </div>

      {/* Bottom Row: Workflow Prompt (full width) */}
      <Card>
        <CardHeader>
          <CardTitle>Workflow Prompt</CardTitle>
          <CardDescription>
            Complete execution and presentation instructions: run scripts, parse
            results, and format output for the user
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Form {...workflowForm}>
            <form
              onSubmit={workflowForm.handleSubmit(onWorkflowSubmit)}
              className="space-y-4"
            >
              <FormField
                control={workflowForm.control}
                name="workflow_prompt"
                render={({ field }) => (
                  <FormItem>
                    <FormControl>
                      <Textarea
                        placeholder="Enter workflow prompt..."
                        className="min-h-[300px]"
                        {...field}
                        value={field.value || ""}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              {workflowForm.formState.isDirty && (
                <div className="flex justify-end gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={handleWorkflowReset}
                    disabled={workflowMutation.isPending}
                  >
                    Reset
                  </Button>
                  <LoadingButton
                    type="submit"
                    loading={workflowMutation.isPending}
                  >
                    Apply Prompt
                  </LoadingButton>
                </div>
              )}
            </form>
          </Form>
        </CardContent>
      </Card>
    </div>
  )
}
