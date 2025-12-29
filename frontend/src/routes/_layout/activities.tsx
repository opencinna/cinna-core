import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState, useMemo, useRef } from "react"

import { ActivitiesService, AgentsService } from "@/client"
import type { ActivityPublicExtended } from "@/client"
import PendingItems from "@/components/Pending/PendingItems"
import { usePageHeader } from "@/routes/_layout"
import { getColorPreset } from "@/utils/colorPresets"
import { RelativeTime } from "@/components/Common/RelativeTime"
import { Bell, CheckCircle2, AlertCircle, FileText, MessageCircle, AlertOctagon } from "lucide-react"
import { cn } from "@/lib/utils"

export const Route = createFileRoute("/_layout/activities")({
  component: ActivitiesList,
})

function ActivitiesList() {
  const { setHeaderContent } = usePageHeader()
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  // Map to track which activities are currently visible
  const [visibleActivities, setVisibleActivities] = useState<Set<string>>(new Set())
  const observerRef = useRef<IntersectionObserver | null>(null)
  const activityRefs = useRef<Map<string, HTMLDivElement>>(new Map())

  const {
    data: activitiesData,
    isLoading: activitiesLoading,
    error: activitiesError,
  } = useQuery({
    queryKey: ["activities", selectedAgentId],
    queryFn: () =>
      ActivitiesService.listActivities({
        agentId: selectedAgentId || undefined,
        skip: 0,
        limit: 100,
        orderDesc: true,
      }),
  })

  const {
    data: agentsData,
    isLoading: agentsLoading,
  } = useQuery({
    queryKey: ["agents"],
    queryFn: () => AgentsService.readAgents({ skip: 0, limit: 100 }),
  })

  const markAsReadMutation = useMutation({
    mutationFn: (activityIds: string[]) =>
      ActivitiesService.markActivitiesAsRead({ requestBody: activityIds }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["activities"] })
      queryClient.invalidateQueries({ queryKey: ["activity-stats"] })
    },
  })

  useEffect(() => {
    setHeaderContent(
      <div className="min-w-0">
        <h1 className="text-lg font-semibold truncate">Activities</h1>
        <p className="text-xs text-muted-foreground">View your system activities and notifications</p>
      </div>
    )
    return () => setHeaderContent(null)
  }, [setHeaderContent])

  // Setup IntersectionObserver to track visible activities
  useEffect(() => {
    observerRef.current = new IntersectionObserver(
      (entries) => {
        const newVisibleActivities = new Set(visibleActivities)

        entries.forEach((entry) => {
          const activityId = entry.target.getAttribute("data-activity-id")
          if (!activityId) return

          if (entry.isIntersecting) {
            newVisibleActivities.add(activityId)
          } else {
            newVisibleActivities.delete(activityId)
          }
        })

        setVisibleActivities(newVisibleActivities)
      },
      {
        threshold: 1.0, // Activity must be fully visible
      }
    )

    return () => {
      observerRef.current?.disconnect()
    }
  }, [])

  // Mark activities as read after 2 seconds of being visible
  useEffect(() => {
    if (visibleActivities.size === 0) return

    const unreadVisibleActivities = Array.from(visibleActivities).filter((activityId) => {
      const activity = activitiesData?.data.find((a) => a.id === activityId)
      return activity && !activity.is_read
    })

    if (unreadVisibleActivities.length === 0) return

    const timer = setTimeout(() => {
      markAsReadMutation.mutate(unreadVisibleActivities)
    }, 2000)

    return () => clearTimeout(timer)
  }, [visibleActivities, activitiesData?.data])

  // Update observer when activities change
  useEffect(() => {
    if (!observerRef.current) return

    // Disconnect old observations
    observerRef.current.disconnect()

    // Observe all activity elements
    activityRefs.current.forEach((element) => {
      if (element) {
        observerRef.current?.observe(element)
      }
    })
  }, [activitiesData?.data])

  const agents = useMemo(() => agentsData?.data || [], [agentsData?.data])

  const getActivityIcon = (activityType: string) => {
    switch (activityType) {
      case "session_completed":
        return <CheckCircle2 className="h-4 w-4" />
      case "questions_asked":
        return <MessageCircle className="h-4 w-4" />
      case "error_occurred":
        return <AlertOctagon className="h-4 w-4" />
      case "file_created":
        return <FileText className="h-4 w-4" />
      case "agent_notification":
        return <Bell className="h-4 w-4" />
      default:
        return <Bell className="h-4 w-4" />
    }
  }

  const handleActivityClick = (activity: ActivityPublicExtended) => {
    if (activity.session_id) {
      navigate({
        to: "/session/$sessionId",
        params: { sessionId: activity.session_id },
      })
    }
  }

  if (activitiesLoading || agentsLoading) {
    return <PendingItems />
  }

  if (activitiesError) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <p className="text-destructive">Error loading activities</p>
      </div>
    )
  }

  const activities = activitiesData?.data || []

  return (
    <div className="p-6 md:p-8 overflow-y-auto h-full">
      <div className="mx-auto max-w-7xl">
        <div className="flex gap-6">
          {/* Filters sidebar */}
          <div className="w-48 flex-shrink-0">
            <div className="sticky top-6 space-y-4">
              <h3 className="text-sm font-semibold text-muted-foreground">Filter by Agent</h3>
              <div className="space-y-2">
                <button
                  onClick={() => setSelectedAgentId(null)}
                  className={cn(
                    "w-full text-left px-3 py-2 text-sm rounded-md transition-colors",
                    selectedAgentId === null
                      ? "bg-primary/10 text-primary font-medium"
                      : "hover:bg-muted"
                  )}
                >
                  All Agents
                </button>
                {agents.map((agent) => {
                  const colorPreset = getColorPreset(agent.ui_color_preset)
                  const isSelected = selectedAgentId === agent.id
                  return (
                    <button
                      key={agent.id}
                      onClick={() => setSelectedAgentId(agent.id)}
                      className={cn(
                        "w-full text-left px-3 py-2 text-sm rounded-md transition-all",
                        colorPreset.badgeBg,
                        colorPreset.badgeText,
                        isSelected && colorPreset.badgeOutline
                      )}
                    >
                      {agent.name}
                    </button>
                  )
                })}
              </div>
            </div>
          </div>

          {/* Activities list */}
          <div className="flex-1">
            {activities.length === 0 ? (
              <div className="text-center py-12 border-2 border-dashed rounded-lg">
                <p className="text-muted-foreground">No activities yet</p>
              </div>
            ) : (
              <div className="space-y-2">
                {activities.map((activity) => {
                  const colorPreset = getColorPreset(activity.agent_ui_color_preset)
                  const hasActionRequired = activity.action_required !== ""
                  const isUnread = !activity.is_read

                  return (
                    <div
                      key={activity.id}
                      ref={(el) => {
                        if (el) {
                          activityRefs.current.set(activity.id, el)
                        } else {
                          activityRefs.current.delete(activity.id)
                        }
                      }}
                      data-activity-id={activity.id}
                      onClick={() => handleActivityClick(activity)}
                      className={cn(
                        "relative p-4 rounded-lg border transition-all",
                        activity.session_id ? "cursor-pointer hover:bg-muted/50" : "",
                        isUnread ? "bg-muted/30" : "bg-card"
                      )}
                    >
                      {/* Unread/Action Required indicator */}
                      {isUnread && (
                        <div
                          className={cn(
                            "absolute top-2 right-2 w-2 h-2 rounded-full",
                            hasActionRequired ? "bg-destructive" : "bg-primary"
                          )}
                        />
                      )}

                      <div className="flex items-start gap-3">
                        {/* Icon */}
                        <div className={cn("mt-1", colorPreset.badgeText)}>
                          {getActivityIcon(activity.activity_type)}
                        </div>

                        {/* Content */}
                        <div className="flex-1 min-w-0 space-y-1">
                          <div className="flex items-center gap-2">
                            {activity.agent_name && (
                              <span
                                className={cn(
                                  "inline-flex items-center px-2 py-0.5 rounded text-xs font-medium",
                                  colorPreset.badgeBg,
                                  colorPreset.badgeText
                                )}
                              >
                                {activity.agent_name}
                              </span>
                            )}
                            {activity.session_title && (
                              <span className="text-xs text-muted-foreground truncate">
                                {activity.session_title}
                              </span>
                            )}
                          </div>
                          <p className="text-sm text-foreground">{activity.text}</p>
                          {hasActionRequired && (
                            <div className="flex items-center gap-1 text-xs text-destructive">
                              <AlertCircle className="h-3 w-3" />
                              <span>Action required</span>
                            </div>
                          )}
                          <RelativeTime
                            timestamp={activity.created_at}
                            className="text-xs text-muted-foreground"
                          />
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
