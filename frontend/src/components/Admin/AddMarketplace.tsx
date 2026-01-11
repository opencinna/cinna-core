import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Plus } from "lucide-react"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { type LLMPluginMarketplaceCreate, LlmPluginsService, SshKeysService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

// Validates both HTTPS and SSH git URLs
const gitUrlPattern = /^(https?:\/\/.+|git@[^:]+:.+)$/

const formSchema = z.object({
  url: z.string().min(1, "Repository URL is required").regex(gitUrlPattern, {
    message: "Must be a valid git URL (HTTPS or SSH format)",
  }),
  ssh_key_id: z.string().optional(),
})

type FormData = z.infer<typeof formSchema>

const AddMarketplace = () => {
  const [isOpen, setIsOpen] = useState(false)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: sshKeys } = useQuery({
    queryKey: ["ssh-keys"],
    queryFn: () => SshKeysService.readSshKeys(),
    enabled: isOpen,
  })

  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    mode: "onBlur",
    criteriaMode: "all",
    defaultValues: {
      url: "",
      ssh_key_id: undefined,
    },
  })

  const mutation = useMutation({
    mutationFn: (data: LLMPluginMarketplaceCreate) =>
      LlmPluginsService.createMarketplace({ requestBody: data }),
    onSuccess: () => {
      showSuccessToast("Marketplace created successfully. Syncing repository...")
      form.reset()
      setIsOpen(false)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["marketplaces"] })
    },
  })

  const onSubmit = (data: FormData) => {
    const submitData: LLMPluginMarketplaceCreate = {
      url: data.url,
      ssh_key_id: data.ssh_key_id === "none" ? undefined : data.ssh_key_id,
    }
    mutation.mutate(submitData)
  }

  return (
    <Dialog open={isOpen} onOpenChange={setIsOpen}>
      <DialogTrigger asChild>
        <Button className="my-4">
          <Plus className="mr-2" />
          Add Marketplace
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add Plugin Marketplace</DialogTitle>
          <DialogDescription>
            Connect a Git repository containing plugin definitions. The
            marketplace name and details will be automatically extracted from
            the repository.
          </DialogDescription>
        </DialogHeader>
        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)}>
            <div className="grid gap-4 py-4">
              <FormField
                control={form.control}
                name="url"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>
                      Repository URL <span className="text-destructive">*</span>
                    </FormLabel>
                    <FormControl>
                      <Input
                        placeholder="git@github.com:user/plugins-repo.git"
                        {...field}
                      />
                    </FormControl>
                    <FormDescription>
                      Git repository URL (HTTPS or SSH format)
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="ssh_key_id"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>SSH Key (for private repos)</FormLabel>
                    <Select
                      onValueChange={field.onChange}
                      value={field.value || "none"}
                    >
                      <FormControl>
                        <SelectTrigger>
                          <SelectValue placeholder="None (for public repos)" />
                        </SelectTrigger>
                      </FormControl>
                      <SelectContent>
                        <SelectItem value="none">None (public repo)</SelectItem>
                        {sshKeys?.data?.map((key) => (
                          <SelectItem key={key.id} value={key.id}>
                            {key.name} ({key.fingerprint.substring(0, 16)}...)
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <DialogFooter>
              <DialogClose asChild>
                <Button variant="outline" disabled={mutation.isPending}>
                  Cancel
                </Button>
              </DialogClose>
              <LoadingButton type="submit" loading={mutation.isPending}>
                Add Marketplace
              </LoadingButton>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  )
}

export default AddMarketplace
