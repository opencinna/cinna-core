import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Plus } from "lucide-react"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  type LLMPluginMarketplaceCreate,
  LlmPluginsService,
  SshKeysService,
} from "@/client"
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
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import {
  MARKETPLACE_FORMAT_VALUES,
  MARKETPLACE_FORMATS,
  marketplaceFormatHelp,
} from "@/utils/marketplace"

// Validates both HTTPS and SSH git URLs
const gitUrlPattern = /^(https?:\/\/.+|git@[^:]+:.+)$/

const formSchema = z.object({
  url: z.string().min(1, "Repository URL is required").regex(gitUrlPattern, {
    message: "Must be a valid git URL (HTTPS or SSH format)",
  }),
  // The tuple, not a third copy of the three strings: it is asserted equal to
  // the generated `LLMPluginMarketplaceCreate["type"]` at compile time, and an
  // unknown value is a 422 on the server rather than a silent fallback to the
  // Claude parser.
  type: z.enum(MARKETPLACE_FORMAT_VALUES),
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
      type: "claude",
      ssh_key_id: undefined,
    },
  })

  const mutation = useMutation({
    mutationFn: (data: LLMPluginMarketplaceCreate) =>
      LlmPluginsService.createMarketplace({ requestBody: data }),
    onSuccess: () => {
      showSuccessToast(
        "Marketplace created successfully. Syncing repository...",
      )
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
      type: data.type,
      ssh_key_id: data.ssh_key_id === "none" ? undefined : data.ssh_key_id,
    }
    mutation.mutate(submitData)
  }

  return (
    <Dialog open={isOpen} onOpenChange={setIsOpen}>
      <DialogTrigger asChild>
        <Button className="my-4">
          <Plus className="mr-2" />
          Add marketplace
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add addon marketplace</DialogTitle>
          <DialogDescription>
            Connect a Git repository of plugins or skills. The marketplace name
            and details are read from the repository itself.
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
                name="type"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Format</FormLabel>
                    <Select onValueChange={field.onChange} value={field.value}>
                      <FormControl>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                      </FormControl>
                      <SelectContent>
                        {MARKETPLACE_FORMATS.map((format) => (
                          <SelectItem key={format.value} value={format.value}>
                            {format.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    {/* The format changes what the syncer looks for and
                        nothing else on this form — which is why it is a field
                        here rather than a type picker (A7): naming the file it
                        expects is the whole difference.

                        `FormDescription` rather than a bare `<p>` so the
                        sentence is the select's `aria-describedby`: it is the
                        only thing on screen that says what the repository must
                        contain, and a reader who never sees it cannot choose. */}
                    <FormDescription className="text-xs">
                      {marketplaceFormatHelp(field.value)}
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
                Add marketplace
              </LoadingButton>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  )
}

export default AddMarketplace
