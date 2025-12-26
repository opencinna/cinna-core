# Frontend Development - LLM Quick Reference

## Toast Notifications
- **USE**: `useCustomToast` hook from `@/hooks/useCustomToast`
- **DO NOT USE**: `@/hooks/use-toast` (does not exist)
- Library: `sonner` (not shadcn/ui toast)
```tsx
import useCustomToast from "@/hooks/useCustomToast"
const { showSuccessToast, showErrorToast } = useCustomToast()
showSuccessToast("Success message")
showErrorToast("Error message")
```

## API Client
- **NEVER manually edit** files in `src/client/`
- Auto-generated from backend OpenAPI spec
- Regenerate after backend changes: `bash scripts/generate-client.sh`
- Import services: `import { UsersService, AgentsService } from "@/client"`

## TanStack Query Patterns
```tsx
// Query
const { data } = useQuery({
  queryKey: ["key"],
  queryFn: () => ServiceName.methodName(),
})

// Mutation
const mutation = useMutation({
  mutationFn: (data) => ServiceName.create({ requestBody: data }),
  onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ["key"] })
    showSuccessToast("Success")
  },
  onError: () => showErrorToast("Error"),
})
```

## Routing
- File-based routing in `src/routes/`
- Protected routes: `src/routes/_layout/` directory
- Route guard pattern:
```tsx
export const Route = createFileRoute("/_layout/path")({
  component: Component,
  beforeLoad: async () => {
    if (!isLoggedIn()) throw redirect({ to: "/login" })
  },
})
```

## Auth
- Hook: `useAuth()` from `@/hooks/useAuth`
- Returns: `{ user, loginMutation, logoutMutation }`
- Access token stored in localStorage: `access_token`
- Check login: `isLoggedIn()` utility function

## Component Libraries
- UI: shadcn/ui components from `@/components/ui/`
- Forms: `react-hook-form` + `zod` validation
- Icons: lucide-react

## State Management
- **Primary**: TanStack Query (no Redux/Zustand)
- **Auth state**: Managed by TanStack Query with `["currentUser"]` key
- **Local state**: React useState

## Common Patterns
- User settings components: `src/components/UserSettings/`
- Settings page uses Tabs component with config array
- Always invalidate queries after mutations
- Use `queryClient.invalidateQueries()` for cache updates

## Environment Variables
- Accessed via `import.meta.env.VITE_*`
- API URL: `import.meta.env.VITE_API_URL`

## Styling
- Tailwind CSS
- Theme: Light/dark mode support via shadcn/ui
- Use `className` prop with Tailwind utilities
- Dynamic classes: Use template literals with full class names (Tailwind JIT)
```tsx
const colorPreset = getColorPreset(agent.ui_color_preset)
<div className={`rounded-lg p-3 ${colorPreset.iconBg}`}>
```

## Dialog/Modal Pattern
```tsx
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"

const [isOpen, setIsOpen] = useState(false)

<Dialog open={isOpen} onOpenChange={setIsOpen}>
  <DialogContent className="sm:max-w-md">
    <DialogHeader>
      <DialogTitle>Title</DialogTitle>
      <DialogDescription>Description</DialogDescription>
    </DialogHeader>
    {/* Content */}
  </DialogContent>
</Dialog>
```

## Utilities Pattern
- Shared constants/helpers: `src/utils/` directory
- Export types and functions
- Example: `src/utils/colorPresets.ts` for color configuration
```tsx
export type ColorPreset = "slate" | "blue" | ...
export const getColorPreset = (preset: string | null | undefined) => { ... }
```

## Tab Components
- Use `HashTabs` component for tabbed interfaces
- Location: `@/components/Common/HashTabs`
```tsx
const tabs = [
  { value: "tab1", title: "Tab 1", content: <Component1 /> },
  { value: "tab2", title: "Tab 2", content: <Component2 /> },
]
<HashTabs tabs={tabs} defaultTab="tab1" />
```
