/**
 * The bordered fact block at the top of an improvement request's Details tab:
 * who sent it and when, then what they wrote. One block, so the form below it
 * is just the decision.
 *
 * Two kinds, two fact sets. A session request counts the messages it captured
 * and calls the text a comment; an API capability request names the consumer
 * agent and the API version the caller saw, and calls the text a need. The
 * text is plain, never markdown: it is cross-user content.
 */
import type { ReactNode } from "react"

import type { ImprovementRequestDetailPublic } from "@/client"
import { RelativeTime } from "@/components/Common/RelativeTime"
import type { ApiCapabilityRequest } from "@/utils/improvementRequests"

function Fact({
  label,
  className,
  children,
}: {
  label: string
  className?: string
  children: ReactNode
}) {
  return (
    <div className={`min-w-0 ${className ?? ""}`}>
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      {children}
    </div>
  )
}

function SecondLine({ children }: { children: ReactNode }) {
  return <dd className="truncate text-xs text-muted-foreground">{children}</dd>
}

interface ImprovementRequestFactsProps {
  request: ImprovementRequestDetailPublic
  /** Set for an API capability request; `null` for a session request. */
  payload: ApiCapabilityRequest | null
}

export function ImprovementRequestFacts({
  request,
  payload,
}: ImprovementRequestFactsProps) {
  const consumerAgent = payload
    ? request.source_agent_name || payload.consumerAgentName
    : ""
  const text = payload ? payload.need || request.comment : request.comment

  return (
    <div className="rounded-md border bg-muted/20">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-3 px-3 py-2.5 text-sm sm:grid-cols-4">
        <Fact label="From" className={payload ? "" : "col-span-2"}>
          <dd className="mt-0.5 truncate font-medium">
            {request.requester_display || "Unknown"}
          </dd>
          {request.requester_email && (
            <SecondLine>{request.requester_email}</SecondLine>
          )}
        </Fact>
        {payload && (
          <Fact label="Consumer agent">
            <dd className="mt-0.5 truncate font-medium">
              {consumerAgent || "Not reported"}
            </dd>
            {payload.consumerTokenLabel && (
              <SecondLine>{payload.consumerTokenLabel}</SecondLine>
            )}
          </Fact>
        )}
        <Fact label="Submitted">
          <dd className="mt-0.5 font-medium">
            <RelativeTime timestamp={request.created_at} />
          </dd>
        </Fact>
        {payload ? (
          <Fact label="API version">
            <dd className="mt-0.5 truncate font-medium">
              {payload.specVersionSeen || "Not reported"}
            </dd>
          </Fact>
        ) : (
          <Fact label="Messages">
            <dd className="mt-0.5 font-medium">
              {request.snapshot_message_count}{" "}
              {request.snapshot_message_count === 1 ? "message" : "messages"}
            </dd>
          </Fact>
        )}
      </dl>
      <div className="border-t px-3 py-2.5">
        <p className="text-xs uppercase tracking-wide text-muted-foreground">
          {payload ? "Need" : "Comment"}
        </p>
        <div className="mt-1 whitespace-pre-wrap break-words text-sm">
          {text || (
            <span className="text-muted-foreground">
              {payload ? "No need was described." : "No comment was left."}
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
