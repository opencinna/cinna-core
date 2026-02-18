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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
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

export function MailServerSettings() {
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

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (serverId: string) =>
      MailServersService.deleteMailServer({ serverId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mail-servers"] })
      showSuccessToast("Mail server deleted successfully")
      setDeleteServerId(null)
    },
    onError: (error: any) => {
      showErrorToast(error?.body?.detail || "Failed to delete mail server")
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
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Mail Servers</CardTitle>
              <CardDescription>
                &nbsp;
              </CardDescription>
            </div>
            <Button onClick={handleAdd} size="sm">
              <Plus className="h-4 w-4 mr-2" />
              Add Server
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="text-sm text-muted-foreground">Loading mail servers...</div>
          ) : servers.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Host</TableHead>
                  <TableHead>Encryption</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {servers.map((server) => (
                  <TableRow key={server.id}>
                    <TableCell className="font-medium">{server.name}</TableCell>
                    <TableCell>
                      <Badge variant="outline" className="uppercase text-xs">
                        {server.server_type}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {server.host}:{server.port}
                    </TableCell>
                    <TableCell>
                      <span className="text-xs text-muted-foreground uppercase">
                        {server.encryption_type || "ssl"}
                      </span>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex gap-1 justify-end">
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-8 w-8"
                                onClick={() => handleTestConnection(server.id)}
                                disabled={testingId === server.id}
                              >
                                {testingId === server.id ? (
                                  <Loader2 className="h-4 w-4 animate-spin" />
                                ) : (
                                  <Plug className="h-4 w-4" />
                                )}
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent>Test connection</TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8"
                          onClick={() => handleEdit(server)}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8"
                          onClick={() => setDeleteServerId(server.id)}
                        >
                          <Trash2 className="h-4 w-4 text-destructive" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <div className="text-sm text-muted-foreground py-6 text-center">
              <Server className="h-8 w-8 mx-auto mb-2 opacity-50" />
              <p>No mail servers configured</p>
              <p className="text-xs mt-1">Add an IMAP or SMTP server to enable email integrations</p>
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
              If this server is used by an email integration, the integration will need to be
              reconfigured.
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
