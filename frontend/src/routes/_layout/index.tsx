import { createFileRoute, Link } from "@tanstack/react-router"

import useAuth from "@/hooks/useAuth"
import { CreateSession } from "@/components/Sessions/CreateSession"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { MessageCircle } from "lucide-react"

export const Route = createFileRoute("/_layout/")({
  component: Dashboard,
  head: () => ({
    meta: [
      {
        title: "Dashboard - FastAPI Cloud",
      },
    ],
  }),
})

function Dashboard() {
  const { user: currentUser } = useAuth()

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl truncate max-w-sm">
          Hi, {currentUser?.full_name || currentUser?.email} 👋
        </h1>
        <p className="text-muted-foreground">
          Welcome back, nice to see you again!!!
        </p>
      </div>

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>Quick Start</CardTitle>
            <CardDescription>
              Start a new conversation with your agent
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <CreateSession variant="default" className="w-full" />
            <Link to="/sessions">
              <Button variant="outline" className="w-full gap-2">
                <MessageCircle className="h-4 w-4" />
                View All Sessions
              </Button>
            </Link>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
