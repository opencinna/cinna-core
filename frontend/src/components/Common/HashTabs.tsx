import { useState, useEffect, ReactNode } from "react"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

export interface TabConfig {
  value: string
  title: string
  content: ReactNode
}

interface HashTabsProps {
  tabs: TabConfig[]
  defaultTab?: string
}

export function HashTabs({ tabs, defaultTab }: HashTabsProps) {
  // Get initial tab from URL hash
  const getInitialTab = () => {
    const hash = window.location.hash.slice(1) // Remove the # character
    const validTabs = tabs.map((tab) => tab.value)
    return validTabs.includes(hash) ? hash : (defaultTab || tabs[0]?.value || "")
  }

  const [activeTab, setActiveTab] = useState(getInitialTab())

  // Update hash when tab changes
  const handleTabChange = (value: string) => {
    setActiveTab(value)
    window.location.hash = value
  }

  // Listen to hash changes (for browser back/forward)
  useEffect(() => {
    const handleHashChange = () => {
      const hash = window.location.hash.slice(1)
      const validTabs = tabs.map((tab) => tab.value)
      if (validTabs.includes(hash)) {
        setActiveTab(hash)
      }
    }

    window.addEventListener("hashchange", handleHashChange)
    return () => window.removeEventListener("hashchange", handleHashChange)
  }, [tabs])

  return (
    <Tabs value={activeTab} onValueChange={handleTabChange}>
      <TabsList>
        {tabs.map((tab) => (
          <TabsTrigger key={tab.value} value={tab.value}>
            {tab.title}
          </TabsTrigger>
        ))}
      </TabsList>
      {tabs.map((tab) => (
        <TabsContent key={tab.value} value={tab.value}>
          {tab.content}
        </TabsContent>
      ))}
    </Tabs>
  )
}
