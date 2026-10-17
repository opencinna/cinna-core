import { useQuery } from "@tanstack/react-query"
import type { ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

/** Loading: a skeleton filling the pane (no spinner, §6). */
export function ViewerSkeleton() {
  return (
    <div className="h-full w-full p-4">
      <Skeleton className="h-full w-full rounded-md" />
    </div>
  )
}

/** A centred sentence in the pane, with an optional action under it. */
export function ViewerMessage({
  children,
  action,
}: {
  children: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
      <p className="max-w-md text-sm text-muted-foreground break-words">
        {children}
      </p>
      {action}
    </div>
  )
}

export function ViewerError({
  name,
  onRetry,
}: {
  name: string
  onRetry: () => void
}) {
  return (
    <ViewerMessage
      action={
        <Button variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      }
    >
      Couldn't load {name}.
    </ViewerMessage>
  )
}

/**
 * A file's text, fetched from its capability URL with no Authorization header
 * (the cap is the credential). Keyed by URL: a new cap is a new read.
 */
export function useFileText(url: string) {
  return useQuery({
    queryKey: ["artifactFile", url],
    queryFn: async ({ signal }) => {
      const response = await fetch(url, { signal })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      return response.text()
    },
    staleTime: Number.POSITIVE_INFINITY,
    refetchOnWindowFocus: false,
    retry: false,
  })
}

/** Loading → error → empty → the caller's render, for a text-backed viewer. */
export function TextFileGate({
  url,
  name,
  children,
}: {
  url: string
  name: string
  children: (text: string) => ReactNode
}) {
  const text = useFileText(url)
  if (text.isError)
    return <ViewerError name={name} onRetry={() => text.refetch()} />
  if (text.data === undefined) return <ViewerSkeleton />
  if (text.data.length === 0)
    return <ViewerMessage>This file is empty.</ViewerMessage>
  return <>{children(text.data)}</>
}
