import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRootRoute, createRoute, createRouter, Outlet, RouterProvider } from '@tanstack/react-router'
import { useForm } from 'react-hook-form'
import { createRoot } from 'react-dom/client'
import { Toaster } from 'sonner'
import { CredentialsService, type CredentialWithData, type CredentialType, type AgentApiKeyPublic } from '@/client'
import { Form } from '@/components/ui/form'
import { Button } from '@/components/ui/button'
import { GenericCredentialForm, ApiTokenCredentialForm, OdooCredentialForm, OAuthCredentialForm, ServiceAccountCredentialForm, SSHKeyEditView } from '@/components/Credentials/CredentialForms'
import { AgentApiConnectionView } from '@/components/Credentials/AgentApiConnectionView'
import { AgentApiKeyView } from '@/components/Credentials/AgentApiKeyView'
import { McpProviderConnectionView } from '@/components/Credentials/McpProviderConnectionView'
import '@/index.css'
const type = new URLSearchParams(location.search).get('type') ?? 'api_token'
const credential = { id: 'credential-1', name: 'Fixture credential', type: (type === 'agent_api_key' ? 'agent_api' : type) as CredentialType, service_uri: 'existing-service', notes: '', credential_data: {}, owner_id: 'owner-1', is_placeholder: true, status: 'incomplete' } as CredentialWithData
function CredentialHost() {
  const form = useForm({ defaultValues: { name: credential.name, service_uri: credential.service_uri, notes: '', credential_data: {} } })
  if (type === 'ssh_key') return <SSHKeyEditView credential={credential} />
  if (type === 'mcp_provider') return <McpProviderConnectionView credential={credential} />
  if (type === 'agent_api') return <AgentApiConnectionView credential={credential} />
  if (type === 'agent_api_key') return <AgentApiKeyView credential={credential} producerAgentId="producer-1" apiKey={{ id: 'key-1', token_prefix: 'fixture', agent_id: 'producer-1', credential_id: 'credential-1', label: 'Fixture', read_only: false, expires_at: null, last_used_at: null, is_active: true, is_usable: true,  created_at: '2026-01-01T00:00:00Z', subject: { id: 'owner-1', email: 'fixture@example.test' } } as unknown as AgentApiKeyPublic} />
  return <Form {...form}><form onSubmit={form.handleSubmit(data => CredentialsService.updateCredential({ id: credential.id, requestBody: data }))} className="space-y-4">
    {type === 'api_token' ? <ApiTokenCredentialForm form={form} /> : type === 'odoo' ? <OdooCredentialForm form={form} /> : type === 'google_service_account' ? <ServiceAccountCredentialForm form={form} /> : type.includes('oauth') ? <OAuthCredentialForm form={form} credentialType={type} credentialId={credential.id} /> : <GenericCredentialForm form={form} credentialType={type} />}
    <Button type="submit">Save</Button>
  </form></Form>
}
const rootRoute=createRootRoute({ component: () => <><main className="max-w-4xl mx-auto p-6"><Outlet /></main><Toaster /></> })
const route=createRoute({ getParentRoute: () => rootRoute, path: '/', component: CredentialHost })
const router=createRouter({ routeTree: rootRoute.addChildren([route]) })
createRoot(document.getElementById('root')!).render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><RouterProvider router={router} /></QueryClientProvider>)
