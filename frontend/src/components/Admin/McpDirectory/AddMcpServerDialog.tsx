import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Globe, type LucideIcon, Radio, Terminal } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { McpDirectoryService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import { getErrorMessage } from "@/utils"
import { McpEntryConnectionFields } from "./McpEntryConnectionFields"
import { McpEntryListingFields } from "./McpEntryListingFields"
import { McpEnvVarsEditor } from "./McpEnvVarsEditor"
import { apiErrorCode } from "./mcpDirectoryApi"
import {
  MCP_DIRECTORY_KEY,
  TRANSPORT_COPY,
  TRANSPORT_ORDER,
} from "./mcpDirectoryCopy"
import {
  emptyDraft,
  hasErrors,
  type McpDraftErrors,
  type McpEntryDraft,
  type McpTransport,
  toCreateBody,
  validateConnection,
  validateListing,
} from "./mcpEntryDraft"

type Step = "type" | "connection" | "listing"

const STEPS: readonly { step: Step; label: string }[] = [
  { step: "type", label: "1 Connection type" },
  { step: "connection", label: "2 Connection" },
  { step: "listing", label: "3 Listing" },
]

const TRANSPORT_ICON: Record<McpTransport, LucideIcon> = {
  http: Globe,
  sse: Radio,
  stdio: Terminal,
}

interface AddMcpServerDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * Add one MCP server to the directory (S5): how it connects, then what it is
 * called. A three-step wizard in one form with hidden panes (the
 * `InviteUserDialog` shape), so typed values survive Back. The caller mounts
 * it fresh per open (`key`), which is the reset.
 */
export function AddMcpServerDialog({
  open,
  onOpenChange,
}: AddMcpServerDialogProps) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [step, setStep] = useState<Step>("type")
  const [draft, setDraft] = useState<McpEntryDraft>(emptyDraft)
  const [errors, setErrors] = useState<McpDraftErrors>({})
  const [nameEdited, setNameEdited] = useState(false)
  const [nameTaken, setNameTaken] = useState<string | undefined>()
  const panes = useRef<Partial<Record<Step, HTMLFieldSetElement | null>>>({})

  const patch = (next: Partial<McpEntryDraft>) => {
    setDraft((prev) => ({ ...prev, ...next }))
    if ("name" in next) setNameTaken(undefined)
  }

  // A step's pane replaces the one whose control was just clicked, so focus
  // moves to the new pane's first control rather than falling to the body.
  useEffect(() => {
    const pane = panes.current[step]
    const target =
      step === "type"
        ? pane?.querySelector<HTMLElement>("[aria-checked=true]")
        : pane?.querySelector<HTMLElement>("input, button, textarea")
    ;(target ?? pane?.querySelector<HTMLElement>("button"))?.focus()
  }, [step])

  const mutation = useMutation({
    mutationFn: () =>
      McpDirectoryService.createMcpDirectoryEntry({
        requestBody: toCreateBody(draft),
      }),
    onSuccess: (entry) => {
      showSuccessToast(`${entry.title} added`)
      void queryClient.invalidateQueries({ queryKey: MCP_DIRECTORY_KEY })
      onOpenChange(false)
    },
    onError: (error) => {
      if (apiErrorCode(error) === "name_taken") {
        setNameTaken("Another server in the directory already uses this name.")
        return
      }
      showErrorToast(getErrorMessage(error, "Couldn't add the MCP server"))
    },
  })
  const pending = mutation.isPending

  const chooseTransport = (transport: McpTransport) => {
    patch({ transport })
    setErrors({})
    setStep("connection")
  }

  const next = () => {
    if (step === "connection") {
      const found = validateConnection(draft)
      setErrors(found)
      if (!hasErrors(found)) setStep("listing")
      return
    }
    if (step === "listing") {
      const found = validateListing(draft, { checkName: true })
      setErrors(found)
      if (!hasErrors(found)) mutation.mutate()
    }
  }

  const back = () => {
    setErrors({})
    setStep(step === "listing" ? "connection" : "type")
  }

  const transport = draft.transport
  const header =
    step === "type"
      ? {
          title: "Add MCP server",
          description: "How does the server connect?",
        }
      : step === "connection" && transport
        ? {
            title: TRANSPORT_COPY[transport].label,
            description: TRANSPORT_COPY[transport].help,
          }
        : {
            title: "Listing",
            description: "What users see when they add it from the directory.",
          }

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!pending) onOpenChange(nextOpen)
      }}
    >
      <DialogContent
        className="max-h-[85vh] overflow-x-hidden overflow-y-auto sm:max-w-lg [&>*]:min-w-0"
        onEscapeKeyDown={(e) => {
          if (pending) e.preventDefault()
        }}
        onInteractOutside={(e) => {
          if (pending) e.preventDefault()
        }}
      >
        {/* One <form> across the steps; Enter means Next on step 2 and Add
            on step 3. Step 1 has only tiles, which advance on click. */}
        <form
          className="grid min-w-0 gap-4"
          onSubmit={(e) => {
            e.preventDefault()
            next()
          }}
        >
          <DialogHeader>
            <p className="text-xs text-muted-foreground">
              {STEPS.map((item, index) => (
                <span key={item.step}>
                  {index > 0 && " · "}
                  <span
                    className={
                      item.step === step
                        ? "font-medium text-foreground"
                        : undefined
                    }
                    aria-current={item.step === step ? "step" : undefined}
                  >
                    {item.label}
                  </span>
                </span>
              ))}
            </p>
            <DialogTitle>{header.title}</DialogTitle>
            <DialogDescription>{header.description}</DialogDescription>
          </DialogHeader>

          <fieldset
            ref={(node) => {
              panes.current.type = node
            }}
            hidden={step !== "type"}
            disabled={pending}
            className="min-w-0"
          >
            <div
              role="radiogroup"
              aria-label="Connection type"
              className="grid gap-2"
            >
              {TRANSPORT_ORDER.map((value) => {
                const Icon = TRANSPORT_ICON[value]
                const selected = transport === value
                return (
                  // biome-ignore lint/a11y/useSemanticElements: a tile with a title and a help line; a native radio cannot hold both.
                  <button
                    key={value}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    onClick={() => chooseTransport(value)}
                    className={cn(
                      "flex items-start gap-3 rounded-md border px-3 py-2.5 text-left transition-colors hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
                      selected && "border-primary bg-primary/5",
                    )}
                  >
                    <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                    <span className="min-w-0">
                      <span className="block text-sm font-medium">
                        {TRANSPORT_COPY[value].label}
                      </span>
                      <span className="block text-xs text-muted-foreground">
                        {TRANSPORT_COPY[value].help}
                      </span>
                    </span>
                  </button>
                )
              })}
            </div>
          </fieldset>

          <fieldset
            ref={(node) => {
              panes.current.connection = node
            }}
            hidden={step !== "connection"}
            disabled={pending}
            className="grid min-w-0 gap-5"
          >
            <McpEntryConnectionFields
              draft={draft}
              onChange={patch}
              errors={errors}
              idPrefix="add-mcp"
            />
            <McpEnvVarsEditor
              value={draft.env}
              onChange={(env) => patch({ env })}
              errors={errors.env}
              countError={errors.envCount}
              idPrefix="add-mcp"
            />
          </fieldset>

          <fieldset
            ref={(node) => {
              panes.current.listing = node
            }}
            hidden={step !== "listing"}
            disabled={pending}
            className="min-w-0"
          >
            <McpEntryListingFields
              draft={draft}
              onChange={patch}
              errors={errors}
              idPrefix="add-mcp"
              nameMode={nameEdited ? "edited" : "auto"}
              onNameEdited={() => setNameEdited(true)}
              optionalFields="collapsed"
              nameError={nameTaken}
            />
          </fieldset>

          <DialogFooter>
            {step === "type" ? (
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
              >
                Cancel
              </Button>
            ) : (
              <>
                <Button
                  type="button"
                  variant="outline"
                  disabled={pending}
                  onClick={back}
                >
                  Back
                </Button>
                {step === "connection" ? (
                  <Button type="submit">Next</Button>
                ) : (
                  <LoadingButton type="submit" loading={pending}>
                    {pending ? "Adding…" : "Add server"}
                  </LoadingButton>
                )}
              </>
            )}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
