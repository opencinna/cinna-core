import { FileText, FileCode, ScrollText, BookOpen, Upload, ChevronDown, Maximize2, Minimize2, KeyRound, Globe, ExternalLink } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

interface TabHeaderProps {
  activeTab: string
  onTabChange: (tab: string) => void
  isWidePanelMode: boolean
  onToggleWidePanel: () => void
  hideCredentials?: boolean
  webappUrl?: string | null
  hasWebapp?: boolean
}

const TAB_LABELS: Record<string, string> = {
  files: "Files",
  scripts: "Scripts",
  logs: "Logs",
  docs: "Docs",
  uploads: "Uploads",
  credentials: "Credentials",
  webapp: "Web App",
}

export function TabHeader({ activeTab, onTabChange, isWidePanelMode, onToggleWidePanel, hideCredentials, webappUrl, hasWebapp }: TabHeaderProps) {
  const menuLabel = TAB_LABELS[activeTab] || "Files"

  return (
    <div className="px-4 pt-3 pb-2 shrink-0 flex items-center gap-2">
      <div className="flex-1">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="secondary"
              size="sm"
              className="w-full justify-between"
            >
              {menuLabel}
              <ChevronDown className="h-3 w-3 ml-1" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className={isWidePanelMode ? "w-[720px]" : "w-[352px]"}>
            <DropdownMenuItem onClick={() => onTabChange("files")}>
              <FileText className="h-4 w-4 mr-2" />
              Files
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => onTabChange("scripts")}>
              <FileCode className="h-4 w-4 mr-2" />
              Scripts
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => onTabChange("logs")}>
              <ScrollText className="h-4 w-4 mr-2" />
              Logs
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => onTabChange("docs")}>
              <BookOpen className="h-4 w-4 mr-2" />
              Docs
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => onTabChange("uploads")}>
              <Upload className="h-4 w-4 mr-2" />
              Uploads
            </DropdownMenuItem>
            {!hideCredentials && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => onTabChange("credentials")}>
                  <KeyRound className="h-4 w-4 mr-2" />
                  Credentials
                </DropdownMenuItem>
              </>
            )}
            {hasWebapp && (
              <DropdownMenuItem onClick={() => onTabChange("webapp")}>
                <Globe className="h-4 w-4 mr-2" />
                Web App Files
              </DropdownMenuItem>
            )}
            {webappUrl && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => window.open(webappUrl, "_blank")}>
                  <Globe className="h-4 w-4 mr-2" />
                  Open Web App
                  <ExternalLink className="h-3 w-3 ml-auto opacity-50" />
                </DropdownMenuItem>
              </>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <Button
        variant="ghost"
        size="sm"
        onClick={onToggleWidePanel}
        title={isWidePanelMode ? "Shrink panel" : "Expand panel"}
        className="shrink-0"
      >
        {isWidePanelMode ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
      </Button>
    </div>
  )
}
