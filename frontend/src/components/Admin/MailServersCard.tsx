/**
 * Mail Servers — server-owned IMAP/SMTP infrastructure, administered from
 * Admin → Server Configuration.
 *
 * A mail server is referenced by id from an email channel's `config`, the way a
 * Google Chat channel references its service account, which is why this sits as
 * a peer of the Channels tab rather than in a user's own settings. The route
 * behind it (`/api/v1/mail-servers/*`) is superuser-only.
 */
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Plus, Pencil, Trash2, Plug, Server, Loader2 } from "lucide-react"
import {
  MailServersService,
  type MailServerConfigPublic,
  type MailServerConfigCreate,
  type MailServerConfigUpdate,
  type MailServerType,
  type EncryptionType,
} from "@/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { Badge } from "@/components/ui/badge"
import useCustomToast from "@/hooks/useCustomToast"

const DEFAULT_PORTS: Record<string, Record<string, number>> = {
  imap: { ssl: 993, tls: 993, starttls: 143, none: 143 },
  smtp: { ssl: 465, tls: 587, starttls: 587, none: 25 },
}

const ENCRYPTION_OPTIONS: { value: EncryptionType; label: string }[] = [
  { value: "ssl", label: "SSL" },
  { value: "tls", label: "TLS" },
  { value: "starttls", label: "STARTTLS" },
  { value: "none", label: "None" },
]

interface MailServerFormData {
  name: string
  server_type: MailServerType
  host: string
  port: number
  encryption_type: EncryptionType
  username: string
  password: string
}

const emptyForm: MailServerFormData = {
  name: "",
  server_type: "imap",
  host: "",
  port: 993,
  encryption_type: "ssl",
  username: "",
  password: "",
}

/**
 * DELIBERATE, TEMPORARY DIVERGENCE from the credential-deletion precedent — Phase 7
 * should converge this on that pattern instead of hand-rolling the shape here.
 *
 * Backend already has `MailServerChannelUsage` / `MailServerDeletionImpact`
 * (backend/app/models/email/mail_server_config.py), but no route declares
 * `response_model=` with them, so they never reach the OpenAPI schema or the
 * generated client (frontend/src/client). Compare the credential precedent:
 * `GET /credentials/{id}/deletion-impact` in backend/app/api/routes/credentials.py
 * is declared with `response_model=CredentialDeletionImpact`, and
 * frontend/src/components/Credentials/DeleteCredential.tsx casts the 409 body to
 * that generated type instead of a local interface.
 *
 * Consequences of the divergence, until Phase 7 fixes it:
 * - Every field below is optional, so a backend rename silently degrades to the
 *   generic "Failed to delete mail server" fallback with no typecheck failure.
 * - This card can only explain the conflict after the failed delete; the
 *   credential precedent's deletion-impact endpoint lets the UI warn the admin
 *   before the destructive click.
 */
interface ChannelUsage {
  channel_name?: string
  role?: string
}

/**
 * Turn a failed delete into something an admin can act on. The in-use case
 * answers 409 with `{ channel_usages: [...] }`; anything else falls back to the
 * usual FastAPI string detail.
 */
function describeDeleteError(error: unknown): string {
  const detail = (error as { body?: { detail?: unknown } })?.body?.detail
  if (typeof detail === "string" && detail) return detail
  const usages = (detail as { channel_usages?: ChannelUsage[] })?.channel_usages
  if (Array.isArray(usages) && usages.length > 0) {
    const names = usages
      .map((u) =>
        u.role ? `${u.channel_name ?? "Unnamed channel"} (${u.role})` : u.channel_name ?? "Unnamed channel",
      )
      .join(", ")
    return `Still used by ${names}. Detach it from the channel before deleting.`
  }
  return "Failed to delete mail server"
}

export function MailServersCard() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [dialogOpen, setDialogOpen] = useState(false)
  const [editingServer, setEditingServer] = useState<MailServerConfigPublic | null>(null)
  const [deleteServerId, setDeleteServerId] = useState<string | null>(null)
  const [formData, setFormData] = useState<MailServerFormData>(emptyForm)
  const [testingId, setTestingId] = useState<string | null>(null)

  // Fetch mail servers
  const { data: serversData, isLoading } = useQuery({
    queryKey: ["mail-servers"],
    queryFn: () => MailServersService.listMailServers(),
  })

  // Create mutation
  const createMutation = useMutation({
    mutationFn: (data: MailServerConfigCreate) =>
      MailServersService.createMailServer({ requestBody: data }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mail-servers"] })
      showSuccessToast("Mail server created successfully")
      setDialogOpen(false)
    },
    onError: (error: any) => {
      showErrorToast(error?.body?.detail || "Failed to create mail server")
    },
  })

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: (data: { serverId: string; body: MailServerConfigUpdate }) =>
      MailServersService.updateMailServer({
        serverId: data.serverId,
        requestBody: data.body,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mail-servers"] })
      showSuccessToast("Mail server updated successfully")
      setDialogOpen(false)
    },
    onError: (error: any) => {
      showErrorToast(error?.body?.detail || "Failed to update mail server")
    },
  })

  // Delete mutation. Deletion is blocked with 409 while an email channel still
  // references the server, and that 409's `detail` is a structured impact
  // object — toasting it raw would print "[object Object]", so the referencing
  // channels are named instead. There is no force: the fix is to detach the
  // server from the channel first.
  const deleteMutation = useMutation({
    mutationFn: (serverId: string) =>
      MailServersService.deleteMailServer({ serverId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mail-servers"] })
      showSuccessToast("Mail server deleted successfully")
      setDeleteServerId(null)
    },
    onError: (error: any) => {
      showErrorToast(describeDeleteError(error))
    },
  })

  // Test connection mutation
  const testMutation = useMutation({
    mutationFn: (serverId: string) =>
      MailServersService.testMailServerConnection({ serverId }),
    onSuccess: (data) => {
      showSuccessToast(data.message || "Connection successful")
      setTestingId(null)
    },
    onError: (error: any) => {
      showErrorToast(error?.body?.detail || "Connection test failed")
      setTestingId(null)
    },
  })

  const handleAdd = () => {
    setEditingServer(null)
    setFormData(emptyForm)
    setDialogOpen(true)
  }

  const handleEdit = (server: MailServerConfigPublic) => {
    setEditingServer(server)
    setFormData({
      name: server.name,
      server_type: server.server_type,
      host: server.host,
      port: server.port,
      encryption_type: server.encryption_type || "ssl",
      username: server.username,
      password: "",
    })
    setDialogOpen(true)
  }

  const handleTestConnection = (serverId: string) => {
    setTestingId(serverId)
    testMutation.mutate(serverId)
  }

  const handleSubmit = () => {
    if (editingServer) {
      // Build update payload (only send changed fields)
      const update: MailServerConfigUpdate = {}
      if (formData.name !== editingServer.name) update.name = formData.name
      if (formData.host !== editingServer.host) update.host = formData.host
      if (formData.port !== editingServer.port) update.port = formData.port
      if (formData.encryption_type !== editingServer.encryption_type)
        update.encryption_type = formData.encryption_type
      if (formData.username !== editingServer.username) update.username = formData.username
      if (formData.password) update.password = formData.password

      updateMutation.mutate({ serverId: editingServer.id, body: update })
    } else {
      createMutation.mutate(formData as MailServerConfigCreate)
    }
  }

  const handleServerTypeChange = (value: MailServerType) => {
    const newPort = DEFAULT_PORTS[value]?.[formData.encryption_type] ?? formData.port
    setFormData({ ...formData, server_type: value, port: newPort })
  }

  const handleEncryptionChange = (value: EncryptionType) => {
    const newPort = DEFAULT_PORTS[formData.server_type]?.[value] ?? formData.port
    setFormData({ ...formData, encryption_type: value, port: newPort })
  }

  const isFormValid = formData.name && formData.host && formData.username && formData.port > 0 &&
    (editingServer ? true : formData.password.length > 0)

  const isSaving = createMutation.isPending || updateMutation.isPending

  const servers = serversData?.data || []

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="flex items-center gap-2">
              <Server className="h-4 w-4 text-blue-500" />
              Mail Servers
            </CardTitle>
            <Button onClick={handleAdd} size="sm">
              <Plus className="h-4 w-4 mr-2" />
              Add
            </Button>
          </div>
          <CardDescription>
            Server-wide IMAP and SMTP servers that email channels send and
            receive through
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <p className="text-sm text-muted-foreground">Loading mail servers...</p>
          ) : servers.length === 0 ? (
            <div className="text-sm text-muted-foreground py-6 text-center">
              <Server className="h-8 w-8 mx-auto mb-2 opacity-50" />
              <p>No mail servers configured</p>
              <p className="text-xs mt-1">Add an IMAP or SMTP server so an email channel has somewhere to send and receive</p>
            </div>
          ) : (
            <div className="space-y-1.5">
              {servers.map((server) => (
                <div
                  key={server.id}
                  className="flex items-center justify-between px-3 py-2 border rounded-lg"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="font-medium text-sm truncate">{server.name}</span>
                    <Badge variant="outline" className="uppercase text-xs shrink-0">
                      {server.server_type}
                    </Badge>
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span className="text-xs text-muted-foreground cursor-help truncate">
                            {server.host}:{server.port}
                          </span>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="text-xs">
                          {server.host}:{server.port} ({(server.encryption_type || "ssl").toUpperCase()})
                        </TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                  </div>
                  <div className="flex items-center gap-1 shrink-0 ml-2">
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7"
                            onClick={() => handleTestConnection(server.id)}
                            disabled={testingId === server.id}
                          >
                            {testingId === server.id ? (
                              <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            ) : (
                              <Plug className="h-3.5 w-3.5" />
                            )}
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent>Test connection</TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7"
                      onClick={() => handleEdit(server)}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7"
                      onClick={() => setDeleteServerId(server.id)}
                    >
                      <Trash2 className="h-3.5 w-3.5 text-destructive" />
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Add/Edit Dialog */}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>
              {editingServer ? "Edit Mail Server" : "Add Mail Server"}
            </DialogTitle>
            <DialogDescription>
              {editingServer
                ? "Update the mail server configuration."
                : "Configure an IMAP or SMTP mail server."}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="name">Name</Label>
              <Input
                id="name"
                placeholder="e.g., Gmail IMAP"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              />
            </div>

            {!editingServer && (
              <div className="space-y-2">
                <Label>Server Type</Label>
                <Select
                  value={formData.server_type}
                  onValueChange={(v) => handleServerTypeChange(v as MailServerType)}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="imap">IMAP (Incoming)</SelectItem>
                    <SelectItem value="smtp">SMTP (Outgoing)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            )}

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="host">Host</Label>
                <Input
                  id="host"
                  placeholder="imap.gmail.com or smtp.gmail.com"
                  value={formData.host}
                  onChange={(e) => setFormData({ ...formData, host: e.target.value })}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="port">Port</Label>
                <Input
                  id="port"
                  type="number"
                  value={formData.port}
                  onChange={(e) => setFormData({ ...formData, port: parseInt(e.target.value) || 0 })}
                />
              </div>
            </div>

            <div className="space-y-2">
              <Label>Encryption</Label>
              <Select
                value={formData.encryption_type}
                onValueChange={(v) => handleEncryptionChange(v as EncryptionType)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ENCRYPTION_OPTIONS.map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label htmlFor="username">Username</Label>
              <Input
                id="username"
                placeholder="user@example.com"
                value={formData.username}
                onChange={(e) => setFormData({ ...formData, username: e.target.value })}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="password">
                Password
                {editingServer && (
                  <span className="text-xs text-muted-foreground ml-2">
                    (leave empty to keep current)
                  </span>
                )}
              </Label>
              <Input
                id="password"
                type="password"
                placeholder={editingServer ? "Enter new password to change" : "Password or app password"}
                value={formData.password}
                onChange={(e) => setFormData({ ...formData, password: e.target.value })}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleSubmit} disabled={!isFormValid || isSaving}>
              {isSaving ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Saving...
                </>
              ) : editingServer ? (
                "Update"
              ) : (
                "Create"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation */}
      <AlertDialog
        open={!!deleteServerId}
        onOpenChange={(open) => !open && setDeleteServerId(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Mail Server</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete this mail server? This action cannot be undone.
              Deletion is refused while an email channel still references the server — detach
              it from the channel first.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => deleteServerId && deleteMutation.mutate(deleteServerId)}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
